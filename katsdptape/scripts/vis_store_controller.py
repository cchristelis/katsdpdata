#!/usr/bin/env python

"""Script for launching the Science Data Processor Vis Store Controller.
   Copyright (c) 2013 SKA/KAT. All Rights Reserved.
"""

import logging
import logging.handlers
import os
import signal
import sys
import Queue

from optparse import OptionParser

if __name__ == "__main__":

    usage = "usage: %prog [options]"
    parser = OptionParser(usage=usage)
    parser.add_option('-a', '--host', dest='host', type="string", default="", metavar='HOST',
                      help='attach to server HOST (default="%default" - localhost)')
    parser.add_option('-p', '--port', dest='port', type="int", default=5001, metavar='N',
                      help='katcp listen port (default=%default)')
    parser.add_option('-w', '--working-folder', dest='workpath',
                      default=os.path.join("/", "var", "kat", "katsdptape"), metavar='WORKING_PATH',
                      help='folder to write process standard out logs into (default=%default)')
    parser.add_option('-l', '--loglevel', dest='loglevel', type="string",
                      default="info", metavar='LOGLEVEL',
                      help='set the Python logging level (default=%default)')
    parser.add_option('-s', '--buffer-size-limit', type="int", default = 2.3*1024*1024*1024, dest="buffer_size",
                      help = 'Soft size limit for buffer which triggers buffer to tape archival (default = %default)')
    (opts, args) = parser.parse_args()

    def die(msg=None):
        if msg:
            print msg
        else:
            parser.print_help()
        sys.exit(1)

    logger = logging.getLogger('katsdptape')
    if isinstance(opts.loglevel, basestring):
        opts.loglevel = getattr(logging, opts.loglevel.upper())
    logger.setLevel(opts.loglevel)
    try:
        fh = logging.handlers.RotatingFileHandler(os.path.join(opts.workpath, 'katsdptape.log'), maxBytes=1e6, backupCount=10)
        formatter = logging.Formatter(("%(asctime)s.%(msecs)dZ - %(name)s - %(filename)s:%(lineno)s - %(levelname)s - %(message)s"),
                                      datefmt="%Y-%m-%d %H:%M:%S")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except IOError:
        logging.basicConfig()
        (logger.warn("Failed to create log file so reverting to console output. Most likely issue is that {0} does not exist or is not writeable"
         .format(os.path.join(opts.workpath))))

    from katsdptape import tapeinterface

    logger.info("Starting SDP Vis Store Controller...")
    print opts.buffer_size
    server = tapeinterface.TapeDeviceServer(opts.host, opts.port, buffer_size=opts.buffer_size)

    restart_queue = Queue.Queue()
    server.set_restart_queue(restart_queue)

    running = True
    def stop_running(signum, frame):
        """Stop the global server."""
        global running
        running = False
    signal.signal(signal.SIGQUIT, stop_running)
    signal.signal(signal.SIGTERM, stop_running)

    server.start()
    logger.info("Started.")

    try:
        while running:
            try:
                device = restart_queue.get(timeout=0.5)
            except Queue.Empty:
                device = None
            if device is not None:
                logger.info("Stopping...")
                device.stop()
                device.join()
                logger.info("Restarting...")
                device.start()
                logger.info("Started.")
    except KeyboardInterrupt:
        pass

    # handle all exit conditions, including keyboard interrupt, katcp halt and sigterm
    server.handle_exit()
    server.stop()
    server.join()