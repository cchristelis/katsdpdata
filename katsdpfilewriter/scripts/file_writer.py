#!/usr/bin/env python

"""Capture L0 visibilities from a SPEAD stream and write to HDF5 file. When
the file is closed, metadata is also extracted from the telescope state and
written to the file. This process lives across multiple observations and
hence multiple HDF5 files.

The status sensor has the following states:

  - `idle`: data is not being captured
  - `capturing`: data is being captured
  - `ready`: CBF data stream has finished, waiting for capture_done request
  - `finalising`: metadata is being written to file
"""

from __future__ import print_function, division
import spead2
import spead2.recv
import katsdptelstate
import time
import os.path
import os
import sys
import socket
import threading
import logging
import Queue
import numpy as np
import signal
import manhole
from katcp import DeviceServer, Sensor
from katcp.kattypes import request, return_reply, Str
from katsdpfilewriter import telescope_model, ar1_model, file_writer


class TelstateModelData(telescope_model.TelescopeModelData):
    """Retrieves metadata from a telescope model. Sensor values
    prior to a given time are excluded.

    Parameters
    ----------
    model : :class:`katsdpfilewriter.telescope_model.TelescopeModel`
        Underlying model
    telstate : :class:`katsdptelstate.TelescopeState`
        Telescope state containing the metadata
    start_timestamp : float
        Minimum timestamp for sensor queries
    """
    def __init__(self, model, telstate, start_timestamp):
        super(TelstateModelData, self).__init__(model)
        self._telstate = telstate
        self._start_timestamp = start_timestamp

    def get_attribute_value(self, attribute):
        return self._telstate.get(attribute.full_name)

    def get_sensor_values(self, sensor):
        try:
            values = self._telstate.get_range(sensor.full_name,
                                              self._start_timestamp,
                                              include_previous=True)
        except KeyError:
            return None
        if values is None:
            return None
        # Reorder fields, and insert a status of 'nominal' since we don't get
        # any status information from the telescope state
        return [(ts, value, 'nominal') for (value, ts) in values]


class FileWriterServer(DeviceServer):
    VERSION_INFO = ("sdp-file-writer", 0, 1)
    BUILD_INFO = ("sdp-file-writer", 0, 1, "rc1")

    def __init__(self, logger, l0_spectral_endpoints, file_base, antenna_mask, telstate, *args, **kwargs):
        super(FileWriterServer, self).__init__(*args, logger=logger, **kwargs)
        self._file_base = file_base
        self._endpoints = l0_spectral_endpoints
        self._capture_thread = None
        self._telstate = telstate
        self._model = ar1_model.create_model(antenna_mask=antenna_mask)
        self._file_obj = None
        self._start_timestamp = None
        self._rx = None

    def setup_sensors(self):
        self._status_sensor = Sensor.string(
                "status", "The current status of the capture process", "", "idle")
        self.add_sensor(self._status_sensor)
        self._filename_sensor = Sensor.string(
                "filename", "Final name for file being captured", "")
        self.add_sensor(self._filename_sensor)
        self._dumps_sensor = Sensor.integer(
                "dumps", "Number of L0 dumps captured", "", [0, 2**63], 0)
        self.add_sensor(self._dumps_sensor)
        self._rate_sensor = Sensor.float(
                "input_rate", "Input data rate in Bps averaged over last 10 dumps", "Bps")
        self.add_sensor(self._rate_sensor)

    def _multicast_socket(self):
        """Returns a socket that is subscribed to any necessary multicast groups."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        for endpoint in self._endpoints:
            if endpoint.multicast_subscribe(sock):
                self._logger.info("Subscribing to multicast address {0}".format(endpoint.host))
            elif endpoint.host != '':
                self._logger.warning("Ignoring non-multicast address {0}".format(endpoint.host))
        return sock

    def _do_capture(self, file_obj):
        """Capture a stream from SPEAD and write to file. This is run in a
        separate thread.

        Parameters
        ----------
        file_obj : :class:`filewriter.File`
            Output file object
        """
        timestamps = []
        sock = self._multicast_socket()
        n_dumps = 0
        n_bytes = 0
        loop_time = time.time()
        try:
            ig = spead2.ItemGroup()
            for heap in self._rx:
                updated = ig.update(heap)
                if 'timestamp' in updated:
                    vis_data = ig['correlator_data'].value
                    flags = ig['flags'].value
                    file_obj.add_data_frame(vis_data, flags)
                    timestamps.append(ig['timestamp'].value)
                    n_dumps += 1
                    n_bytes += vis_data.nbytes + flags.nbytes
                    self._dumps_sensor.set_value(n_dumps)
                    if n_dumps % 10 == 0 and n_dumps > 0:
                        self._rate_sensor.set_value(n_bytes / (time.time() - loop_time))
                        n_bytes = 0
        except Exception as err:
            self._logger.error(err)
        finally:
            self._status_sensor.set_value("ready")
            sock.close()
            # Timestamps in the SPEAD stream are relative to sync_time
            if not timestamps:
                self._logger.warning("H5 file contains no data and hence no timestamps")
            else:
                timestamps = np.array(timestamps) + self._telstate.cbf_sync_time
                file_obj.set_timestamps(timestamps)
                self._logger.info('Set %d timestamps', len(timestamps))

    @request()
    @return_reply(Str())
    def request_capture_init(self, req):
        """Start listening for L0 data and write it to HDF5 file."""
        if self._capture_thread is not None:
            self._logger.info("Ignoring capture_init because already capturing")
            return ("fail", "Already capturing")
        timestamp = time.time()
        self._final_filename = os.path.join(
                self._file_base, "{0}.h5".format(int(timestamp)))
        self._stage_filename = os.path.join(
                self._file_base, "{0}.writing.h5".format(int(timestamp)))
        self._filename_sensor.set_value(self._final_filename)
        self._status_sensor.set_value("capturing")
        self._dumps_sensor.set_value(0)
        self._file_obj = file_writer.File(self._stage_filename)
        self._start_timestamp = timestamp
        self._rx = spead2.recv.Stream(spead2.ThreadPool(), bug_compat=spead2.BUG_COMPAT_PYSPEAD_0_5_2)
        self._rx.add_udp_reader(self._endpoints[0].port)
        self._capture_thread = threading.Thread(
                target=self._do_capture, name='capture', args=(self._file_obj,))
        self._capture_thread.start()
        self._logger.info("Starting capture to %s", self._stage_filename)
        return ("ok", "Capture initialised to {0}".format(self._stage_filename))

    @request()
    @return_reply(Str())
    def request_capture_done(self, req):
        """Stop capturing and close the HDF5 file, if it is not already done."""
        if self._capture_thread is None:
            self._logger.info("Ignoring capture_done because already explicitly stopped")
        return self.capture_done()

    def capture_done(self):
        """Implementation of :meth:`request_capture_done`, split out to allow it
        to be called on `KeyboardInterrupt`.
        """
        if self._capture_thread is None:
            return ("fail", "Not capturing")
        self._rx.stop()
        self._capture_thread.join()
        self._capture_thread = None
        self._rx = None
        self._logger.info("Joined capture thread")

        self._status_sensor.set_value("finalising")
        self._file_obj.set_metadata(TelstateModelData(
                self._model, self._telstate, self._start_timestamp))
        self._file_obj.close()
        self._file_obj = None
        self._start_timestamp = None
        self._logger.info("Finalised file")

        # File is now closed, so rename it
        try:
            os.rename(self._stage_filename, self._final_filename)
            result = ("ok", "File renamed to {0}".format(self._final_filename))
        except OSError as e:
            logger.error("Failed to rename output file %s to %s",
                         self._stage_filename, self._final_filename, exc_info=True)
            result = ("fail", "Failed to rename output file from {0} to {1}.".format(
                self._stage_filename, self._final_filename))
        self._status_sensor.set_value("idle")
        return result

def comma_list(type_):
    """Return a function which splits a string on commas and converts each element to
    `type_`."""

    def convert(arg):
        return [type_(x) for x in arg.split(',')]
    return convert

def main():
    if len(logging.root.handlers) > 0: logging.root.removeHandler(logging.root.handlers[0])
    formatter = logging.Formatter("%(asctime)s.%(msecs)dZ - %(filename)s:%(lineno)s - %(levelname)s - %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logging.root.addHandler(sh)

    logger = logging.getLogger("katsdpfilewriter")
    logger.setLevel(logging.INFO)
    logging.getLogger('spead2').setLevel(logging.WARNING)

    parser = katsdptelstate.ArgumentParser()
    parser.add_argument('--l0-spectral-spead', type=katsdptelstate.endpoint.endpoint_list_parser(7200), default=':7200', help='source port/multicast groups for spectral L0 input. [default=%(default)s]', metavar='ENDPOINTS')
    parser.add_argument('--file-base', default='.', type=str, help='base directory into which to write HDF5 files. [default=%(default)s]', metavar='DIR')
    parser.add_argument('--antenna-mask', type=comma_list(str), default='', help='List of antennas to store in the telescope model. [default=%(default)s]')
    parser.add_argument('-p', '--port', dest='port', type=int, default=2046, metavar='N', help='katcp host port. [default=%(default)s]')
    parser.add_argument('-a', '--host', dest='host', type=str, default="", metavar='HOST', help='katcp host address. [default=all hosts]')
    parser.set_defaults(telstate='localhost')
    args = parser.parse_args()
    if not os.access(args.file_base, os.W_OK):
        logger.error('Target directory (%s) is not writable', args.file_base)
        sys.exit(1)

    restart_queue = Queue.Queue()
    server = FileWriterServer(logger, args.l0_spectral_spead, args.file_base, args.antenna_mask, args.telstate,
                              host=args.host, port=args.port)
    server.set_restart_queue(restart_queue)
    server.start()
    logger.info("Started file writer server.")


    manhole.install(oneshot_on='USR1', locals={'server':server, 'args':args})
     # allow remote debug connections and expose server and args

    def graceful_exit(_signo=None, _stack_frame=None):
        logger.info("Exiting filewriter on SIGTERM")
        os.kill(os.getpid(), signal.SIGINT)
         # rely on the interrupt handler around the katcp device server
         # to peform graceful shutdown. this preserves the command
         # line Ctrl-C shutdown.

    signal.signal(signal.SIGTERM, graceful_exit)
     # mostly needed for Docker use since this process runs as PID 1
     # and does not get passed sigterm unless it has a custom listener

    try:
        while True:
            try:
                device = restart_queue.get(timeout=0.5)
            except Queue.Empty:
                device = None
            if device is not None:
                logger.info("Stopping")
                device.capture_done()
                device.stop()
                device.join()
                logger.info("Restarting")
                device.start()
                logger.info("Started")
    except KeyboardInterrupt:
        logger.info("Shutting down file_writer server...")
        logger.info("Activity logging stopped")
        server.capture_done()
        server.stop()
        server.join()

if __name__ == '__main__':
    main()
