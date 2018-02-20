#!/usr/bin/env python

"""Serialise a view of the Telescope State for the current observation to long term storage.

The Telescope State (TS) is a meta-data repository that includes information about the
current state of the wide telescope, configuration data and intermediate SDP products
such as calibration solutions. It also contains references to the data objects that
comprise the visibility data captured for an observation.

The MeerKAT data access library (katdal) uses the TS effectively as the 'file'
representation of an observation. Such a 'file' can be opened by pointing katdal to either
a live TS repository (currently Redis backed) or to a serialised representation
of the TS (currently supports Redis RDB format).

This writer, when requested, can create two different views of the TS and save these into
long term storage.

The first view is a lightweight representation of the TS containing the basic data
to allow katdal to open an observation. This includes data such as captured timestamps,
storage configuration information and observation targets. Using the supplied capture block
ID, an attempt is also made to only record meta information specific to that capture
block ID.

The second is a complete dump of the entire TS, in the Redis case using the BGSAVE
command to produce a complete RDB file. This may contain meta-data from other capture sessions.
"""

import os
import socket
import sys
import errno
import logging
import asyncio
import signal
import time
from concurrent.futures import ProcessPoolExecutor

import boto
import boto.s3.connection
import katsdpservices
import katsdpfilewriter
from katsdptelstate.rdb_writer import RDBWriter
from aiokatcp import DeviceServer, Sensor, FailReply

# Template of key names that we would like to preserve when dumping
# a lite version of Telstate. Since we always back observations with
# a full dump of Telstate, we don't fail on missing entries, but do
# log them.
# Each string entry is format'ed with the following substitutions:
#
#    {cb}: The capture block ID which uniquely identifies this data capture
#    {sn}: The name of a specific stream in the capture (e.g. sdp_l0)
#    m???: A special that represents a glob-style pattern match used to
#          do wildcard antenna name matching for certain keys.
#
LITE_KEYS = [
    "{sn}_int_time",
    "{sn}_bls_ordering",
    "{sn}_n_chans",
    "{sn}_bandwidth",
    "{sn}_center_freq",
    "{sn}_ceph_conf",
    "{sn}_ceph_pool",
    "{sn}_s3_endpoint",
    "{cb}_{sn}_timestamps",
    "{cb}_{sn}_correlator_data",
    "{cb}_{sn}_weights",
    "{cb}_{sn}_weights_channel",
    "{cb}_{sn}_flags",
    "obs_params",
    "obs_script_log",
    "obs_label",
    "sub_pool_resources",
    "sub_band",
    "sub_product",
    "m???_observer",
    "m???_activity",
    "m???_target",
    "cbf_target"
]


def make_boto_dict(s3_args):
    """Create a dict of keyword parameters suitable for passing into a boto.connect_s3 call using the supplied args."""
    return {
            "aws_access_key_id": s3_args.access_key,
            "aws_secret_access_key": s3_args.secret_key,
            "host": s3_args.s3_host,
            "port": s3_args.s3_port,
            "is_secure": False,
            "calling_format": boto.s3.connection.OrdinaryCallingFormat()
           }


def generate_lite_keys(telstate, capture_block_id, stream_name):
    """Uses capture_block_id and stream_name, along with the template
    of keys to store in the lite dump, to build a full list of the keys
    to be dumped.
    Note: We avoid using telstate views here since we want to write fully
    qualified keys into the lite database to easily allow merge later on.
    The philosophy of the lite dump is to change as little as possible.
    """
    keys = []
    for key in LITE_KEYS:
        if key.find('?') >= 0:
            keys.extend(telstate.keys(filter=key))
        else:
            keys.append(key.format(cb=capture_block_id, sn=stream_name))
    return keys


def get_s3_connection(boto_dict):
    """Test the connection to S3 as described in the args, and return
    the current user id and the connection object.

    Returns
    -------
    s3_conn : S3Connection
        A connection to the s3 endpoint. None if a connection error occurred.
    """
    s3_conn = boto.connect_s3(**boto_dict)
    try:
        s3_conn.get_canonical_user_id()
         # reliable way to test connection and access keys
        return s3_conn
    except socket.error as e:
        if e.errno != errno.ECONNREFUSED:
            raise e
        logger.error("Failed to connect to S3 host %s:%s. Please check network and host address.", boto_dict['host'], boto_dict['port'])
    except boto.exception.S3ResponseError as e:
        if e.error_code == u'InvalidAccessKeyId':
            logger.error("Supplied access key %s is not a valid S3 user.", boto_dict['aws_access_key_id'])
        if e.error_code == u'SignatureDoesNotMatch':
            logger.error("Supplied secret key is not valid for specified user.")
        if e.status == 403 or e.status == 409:
            logger.error("Supplied access key (%s) has no permissions on this server.", boto_dict['aws_access_key_id'])
    return None


def _write_lite_rdb(telstate, capture_block_id, stream_name, bucket_prefix):
    keys = generate_lite_keys(telstate, capture_block_id, stream_name)
    dump_folder = os.path.join(bucket_prefix, capture_block_id)
    dump_filename = os.path.join(dump_folder, "{}_{}.rdb".format(capture_block_id, stream_name))
    if not os.path.exists(dump_folder):
        try:
            os.mkdir(dump_folder)
        except OSError as e:
            logger.error("Failed to create dump folder %s", dump_folder)
            return None
    logger.info("Writing {} keys to local RDB dump {}".format(len(keys), dump_filename))

    rdbw = RDBWriter(client=telstate._r)
    (written, errors) = rdbw.save(dump_filename, keys=keys)

    logger.info("Write complete. {} errors".format(errors))
    return dump_filename


def _store_lite_rdb(bucket_name, dump_filename, boto_dict):
    s3_conn = get_s3_connection(boto_dict)
    key_name = os.path.basename(dump_filename)
    file_size = os.path.getsize(dump_filename)

    if not s3_conn:
        logger.error("Unable to store RDB dump in S3.")
        return None
    written_bytes = 0
    try:
        s3_conn.create_bucket(bucket_name)
        bucket = s3_conn.get_bucket(bucket_name)
        k = bucket.new_key(key_name)
        written_bytes = k.set_contents_from_filename(dump_filename)
    except boto.exception.S3ResponseError as e:
        if e.status == 409:
            logger.error("Unable to store RDP dump as access key %s does not have permission to write to bucket %s",
                         boto_dict["aws_access_key_id"], bucket_name)
            return None
        if e.status == 404:
            logger.error("Unable to store RDB dump as the bucket %s or key %s has been lost.", bucket_name, key_name)
            return None
    if written_bytes != file_size:
        logger.error("Incorrect number of bytes written (%d/%d) when writing RDB dump %s", written_bytes, file_size, dump_filename)
        return None
    return written_bytes


class MetaWriterServer(DeviceServer):
    VERSION = "sdp-meta-writer-0.1"
    BUILD_STATE = "katsdpfilewriter-" + katsdpfilewriter.__version__

    def __init__(self, host, port, loop, executor, logger, boto_dict, rdb_path, telstate):
        self._boto_dict = boto_dict
        self._loop = loop
        self._async_task = None
        self._executor = executor
        self._rdb_path = rdb_path
        self._telstate = telstate

        self._build_state_sensor = Sensor(str, "build-state", "SDP Controller build state.")

        self._device_status_sensor = Sensor(str, "status", "The current status of the meta writer process")
        self._last_write_stream_sensor = Sensor(str, "last-write-stream", "The stream name of the last meta data dump.")
        self._last_write_cbid_sensor = Sensor(str, "last-write-cbid", "The capture block ID of the last meta data dump.")

        super().__init__(host, port)

        self.sensors.add(self._device_status_sensor)
        self.sensors.add(self._last_write_stream_sensor)
        self.sensors.add(self._last_write_cbid_sensor)

    @property
    def async_busy(self):
        """Whether there is an asynchronous state-change operation in progress."""
        return self._async_task is not None and not self._async_task.done()

    def _fail_if_busy(self):
        """Raise a FailReply if there is an asynchronous operation in progress."""
        if self.async_busy:
            raise FailReply('Meta-data writer is busy with an operation. Please wait for it to complete first.')

    def _clear_async_task(self, future):
        """Clear the current async task.

        Parameters
        ----------
        future : :class:`asyncio.Future`
            The expected value of :attr:`_async_task`. If it does not match,
            it is not cleared (this can happen if another task replaced it
            already).
        """
        if self._async_task is future:
            self._async_task = None

    def _write_meta(self, capture_block_id, stream_name, light=True):
        """Write meta-data extracted from the current telstate object
        to a binary dump and place this in the currently connected
        S3 bucket for storage.
        """
        dump_filename = yield from loop.run_in_executor(None, _write_lite_rdb, self._telstate, capture_block_id, stream_name, self._rdb_path)
        if not dump_filename:
            raise FailReply("Failed to write Telstate keys to RDB file.")

        written_b = yield from loop.run_in_executor(None, _store_lite_rdb, capture_block_id, dump_filename, boto_dict)
         # write RDB into S3 - note that capture_block_id is used as the bucket name for storing meta-data
         # regardless of the stream selected.
         # The full capture_block_stream_name is used as the bucket for payload data for the particular stream.

        if not written_b:
            logger.error("Failed to store RDB dump {} in S3 endpoint".format(dump_filename))
        else:
            logger.info("RDB file written to bucket %s with key %s", capture_block_id, os.path.basename(dump_filename))

        return written_b

    async def write_light_meta(self, capture_block_id, stream_name):
        """Implementation of request_write_light_meta."""
        task = asyncio.ensure_future(self._write_meta(capture_block_id, stream_name, light=True), loop=self._loop)
        self._async_task = task
        try:
            written_b = await task
        finally:
            self._clear_async_task(task)
        return written_b

    async def request_write_light_meta(self, ctx, capture_block_id: str, stream_name: str) -> str:
        """Write a lightweight variant of the currently active telescope state to the already
        specified S3 bucket. If a capture_block_id is specified, this is used to produce a
        view on the telstate object specific to that block.
        Method may take some time so is run asychronously.

        Parameters
        ----------
        capture_block_id : string
            The capture block id generated by master controller to identify a
            specific data capture. Typically this will be an integer representing the start time of
            the observation in epoch seconds (+/- to allow for uniqueness if required).
        stream_name : string
            The specific stream name to use in extracting stream specific meta-data. (e.g. sdp_l0)

        Returns
        -------
        success : {'ok', 'fail'}
            Whether the command succeeded
        timing : str
            The capture duration (and resultant MBps)
        """
        self._fail_if_busy()
        ctx.inform("Starting write of lightweight metadata for CB: %s and Stream: %s to S3. This may take a minute or two...",
                   capture_block_id, stream_name)
        st = time.time()
        written_b = await self.write_light_meta(capture_block_id, stream_name)
        if not written_b:
            return "Lightweight meta-data for CB: {}_{} written to local disk only. File is *not* in S3, \
                    but will be moved independently once the link is restored".format(capture_block_id, stream_name)
        duration_s = time.time() - st
        return "Lightweight meta-data for CB: {}_{} written to S3 in {}s @ {}MBps".format(capture_block_id, stream_name, duration_s, written_b / 1e6 / duration_s)


def on_shutdown(loop, server):
    loop.remove_signal_handler(signal.SIGINT)
    loop.remove_signal_handler(signal.SIGTERM)
     # in case the exit code below borks, we allow shutdown via traditional means
    server.halt()


async def run(loop, server):
    await server.start()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, lambda: on_shutdown(loop, server))
    await server.join()


if __name__ == '__main__':
    katsdpservices.setup_logging()
    logger = logging.getLogger("katsdpmetawriter")
    katsdpservices.setup_restart()

    parser = katsdpservices.ArgumentParser()
    parser.add_argument('--rdb-path', default="/var/kat/data", metavar='RDBPATH',
                        help='Root in which to write RDB dumps.')
    parser.add_argument('--access-key', default="", metavar='ACCESS',
                        help='S3 access key with write permission to the specified bucket. Default is unauthenticated access')
    parser.add_argument('--secret-key', default="", metavar='SECRET',
                        help='S3 secret key for the specified access key. Default is unauthenticated access')
    parser.add_argument('--s3_host', default='localhost', metavar='HOST',
                        help='S3 gateway host address [default=%(default)s]')
    parser.add_argument('--s3_port', default=7480, metavar='PORT',
                        help='S3 gateway port [default=%(default)s]')
    parser.add_argument('-p', '--port', type=int, default=2047, metavar='N',
                        help='KATCP host port [default=%(default)s]')
    parser.add_argument('-a', '--host', default="", metavar='HOST',
                        help='KATCP host address [default=all hosts]')

    args = parser.parse_args()

    if not os.path.exists(args.rdb_path):
        logger.error("Specified RDB path, %s, does not exist.", args.rdb_path)
        sys.exit(2)

    boto_dict = make_boto_dict(args)
    logger.info("Using boto dict: {}".format(boto_dict))
    s3_conn = get_s3_connection(boto_dict)
    if not s3_conn:
        logger.error("Exiting due to failure to establish connection to S3 endpoint")
         # get_s3_connection will have already logged the reason for failure
        sys.exit(2)

    user_id = s3_conn.get_canonical_user_id()
    s3_conn.close()
     # we rebuild the connection each time we want to write a meta-data dump

    logger.info("Successfully tested connection to S3 endpoint as %s.", user_id)

    loop = asyncio.get_event_loop()
    executor = ProcessPoolExecutor()

    server = MetaWriterServer(args.host, args.port, loop, executor, logger, boto_dict, args.rdb_path, args.telstate)
    logger.info("Started meta-data writer server.")

    loop.run_until_complete(run(loop, server))
    loop.close()
