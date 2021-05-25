#!/usr/bin/env python3

"""Parallel file uploader to trawl NPY files into S3."""

import boto
import boto.s3.connection
import concurrent.futures as futures
import json
import katsdpservices
import logging
import multiprocessing
import os
import pysolr
import re
import sys
import socket
import shutil
import time

from katsdpdata.met_detectors import file_type_detection
from katsdpdata.met_extractors import MetExtractorException
from katsdpdata.met_handler import MetaDataHandler
from katsdpdata.prod_handler import get_s3_connection
from katsdpdata.prod_handler import make_boto_dict
from katsdpdata.prod_handler import redact_key
from optparse import OptionParser

CAPTURE_BLOCK_REGEX = "^[0-9]{10}$"
CAPTURE_STREAM_L0_REGEX = "^[0-9]{10}[-_].*l0$"
CAPTURE_STREAM_L1_REGEX = "^[0-9]{10}[-_].*l1-flags$"
MAX_TRANSFERS = 5000
CPU_MULTIPLIER = 10
SLEEP_TIME = 20


class Product:

    def __init__(self, product_path):
        """
        :param product_path: string : The directory to trawl. Usually the
                                     sub-directory below the top level
                                     trawl directory.
        """
        self.product_path = product_path
        self.file_matches = []
        self.complete = None

    @staticmethod
    def set_failed_token(prod_dir, msg=None):
        """Set a failed token for the given product directory.

        Parameters
        ----------
        prod_dir: string : The full path to the directory to put the token into.
        msg : string : an optinal message to write into the failed token.
        """
        failed_token_file = os.path.join(prod_dir, "failed")
        if not os.path.isfile(failed_token_file):
            logger.warning("Exception: %s from future.", msg)
            if not msg:
                msg = ""
            with open(failed_token_file, "w") as failed_token:
                failed_token.write(msg)

    def transfer_files(self, trawl_dir, boto_dict, file_list):
        """Transfer file list to s3.

        Parameters
        ----------
        trawl_dir: string : The full path to the trawl directory
        boto_dict: dict : parameter dict for boto connection.
        file_list: list : a list of full path to files to transfer.

        Returns
        -------
        transfer_list: list : a list of s3 URLs that where transfered.
        """
        s3_conn = get_s3_connection(boto_dict)
        bucket = None
        transfer_list = []
        for filename in file_list:
            bucket_name, key_name = os.path.relpath(filename, trawl_dir).split(
                "/", 1)
            file_size = os.path.getsize(filename)
            if not bucket or bucket.name != bucket_name:
                bucket = self.s3_create_bucket(s3_conn, bucket_name)
            key = bucket.new_key(key_name)
            res = key.set_contents_from_filename(filename)
            if res == file_size:
                os.unlink(filename)
                transfer_list.append("/".join(["s3:/", bucket.name, key.name]))
            else:
                logger.error("%s not deleted. Only uploaded %i of %i bytes.",
                             filename, res, file_size)
        return transfer_list

    @staticmethod
    def s3_create_bucket(s3_conn, bucket_name):
        """Create an s3 bucket. If S3CreateError and the error
        status is 409, return a referece to the bucket as it has
        already been created and is owned by you.
        Returns
        ------
        s3_bucket : boto.s3.bucket.Bucket
            An S3 Bucket object
        """
        s3_bucket_policy = s3_create_anon_access_policy(bucket_name)
        try:
            s3_bucket = s3_conn.create_bucket(bucket_name)
            s3_bucket.set_policy(s3_bucket_policy)
        except boto.exception.S3ResponseError as e:
            if e.status == 403 or e.status == 409:
                logger.error(
                    "Error status %s. Supplied access key (%s) has no permissions on this server.",
                    e.status, redact_key(s3_conn.access_key))
            raise
        except boto.exception.S3CreateError as e:
            if e.status == 409:  # Bucket already exists and you're the owner
                s3_bucket = s3_conn.get_bucket(bucket_name)
            else:
                raise
        return s3_bucket

    def _discover_trawl_files(
            self, file_match, file_writing, complete_token,
            time_out=10):
        """Return a list of all trawled files in a directory. Files need to
        match the glob pattern. Also, add the complete token if found. Timeout
        after a while, we're going to trim the upload list anyway.

        Move products with a failed token into the failed directory.

        Parameters
        ----------
        file_match: string : The glob file to match. E.g. '*.npy' or '*.rdb'
        file_writing: string: The glob file to skip since its still being
                             written to. E.g. '*.writing.npy' or '*.writing.rdb'
        complete_token: string : The complete stream complete token to look
            for in the trawled dir.
        time_out: int : Maximum allowed time to perform action

        Returns
        -------
        file_matches: list : A list of all the matching files for the file glob
            and complete token.
        complete: boolean : True if complete token detected.
        """
        prod_dir = os.path.abspath(self.product_path)
        start_time = time.time()
        file_ext = file_match[1:]  # Turn glob into file extension
        write_ext = file_writing[1:]
        # check for failed token, if there return an empty list and incomplete.
        if os.path.isfile(os.path.join(prod_dir, "failed")):
            logger.warning("%s so not processing, moving to failed directory.",
                           os.path.join(prod_dir, "failed"))
            # move product to failed dir
            failed_dir = os.path.join(os.path.split(prod_dir)[0], "failed")
            if not os.path.isdir(failed_dir):
                os.mkdir(failed_dir)
            shutil.move(prod_dir, failed_dir)
            return
        self.complete = False
        for root, dirnames, filenames in os.walk(prod_dir):
            for filename in filenames:
                if filename.endswith(write_ext):
                    # still being written to; ignore
                    continue
                elif filename.endswith(file_ext):
                    self.file_matches.append(os.path.join(root, filename))
                elif filename.endswith(complete_token):
                    self.complete = True
                else:
                    continue
            time_check = time.time() - start_time
            if time_check > time_out:
                break

    def completed_and_transferred(self):
        """True when the complete token is present and the folder empty.

        :return: boolean : True if transfer is complete else False
        """
        return self.complete and not self.file_matches

    def cleanup(self):
        """Recursive delete the product directory supplied directory.
        Should be a completed product."""
        logger.info("%s is complete. Deleting directory tree.", self.product_path)
        return shutil.rmtree(self.product_path)

    def transfer(self, trawl_dir, solr_url):
        if not self.file_matches:
            return 'Empty'

        # find all unique products
        rdb_prods = list(set([re.match('^.*[0-9]{10}_[^.]*', cbf).group()
                         for cbf in self.file_matches
                         if re.match('^.*[0-9]{10}_[^.]*', cbf) is not None]))
        # keep track of when a product failes to ingest. Break out of the loop at the first
        # failure so that the directory is listed again and the failed token is detected.
        # list the rdb prods in order, so that if l0 is broken
        # product directory is marked as failed and no further streams are transferred.
        for rdb_prod in sorted(rdb_prods):
            rdb_lite, rdb_full = rdb_prod + '.rdb', rdb_prod + '.full.rdb'
            if rdb_lite in self.file_matches and rdb_full in self.file_matches:
                try:
                    try:
                        prod_met_extractor = file_type_detection(rdb_lite)
                    except Exception as err:
                        bucket_name = os.path.relpath(rdb_lite, trawl_dir).split("/", 1)[0]
                        err.bucket_name = bucket_name
                        err.filename = rdb_lite
                        raise
                    met = ingest_vis_product(
                        trawl_dir, os.path.relpath(rdb_prod, self.self.product_path),
                        [rdb_lite, rdb_full], prod_met_extractor, solr_url)
                    logger.info(
                        '%s ingested into archive with datastore refs:%s.' %
                        (met['id'], ', '.join(met['CAS.ReferenceDatastore'])))
                except Exception as err:
                    if hasattr(err, 'bucket_name'):
                        logger.exception("Caught exception while extracting metadata from %s.", err.filename)
                        self.set_failed_token(os.path.join(trawl_dir, err.bucket_name), str(err))
                        # if failed, set a boolean flag to exit the loop.
                        return 'Failed'
                    else:
                        raise

        return "Completed"

    @staticmethod
    def product_regex():
        pass


class RDBProduct(Product):
    # Thinking of this as a product might not be technically correct,
    # but it makes this implementation easier.
    regex = CAPTURE_BLOCK_REGEX

    def __init__(self, product_path):
        super().__init__(product_path)

    def discover_trawl_files(self):
        super()._discover_trawl_files('*.rdb', '*.writing.rdb', 'complete')


class L0Product(Product):
    regex = CAPTURE_STREAM_L0_REGEX

    def discover_trawl_files(self):
        super()._discover_trawl_files('*.npy', '*.writing.npy', 'complete')


class L1Product(Product):
    regex = CAPTURE_STREAM_L1_REGEX

    def discover_trawl_files(self):
        super()._discover_trawl_files('*.npy', '*.writing.npy', 'complete')


class ProductFactory:
    def __init__(self, trawl_dir):
        """Creates three lists, that have a valid signature for:
            (1) capture blocks and
            (2) capture l0 streams
            (3) capture l1 streams
        It's useful to seperate out capture blocks and capture stream
        directories, as they are processed differently.

        Parameters
        ----------
        trawl_dir: string : The full path to the trawl directory.

        Returns
        -------
        tuple: (capture_block_dirs, capture_stream_dirs,)
        capture_block_dirs: list : full path to valid capture block directories
        capture_stream_dirs: list : full path to valid capture stream directories
        """
        # get full path to capture block dirs
        self.capture_block_dirs = self._list_dir_helper(trawl_dir, CAPTURE_BLOCK_REGEX)
        # get full path to capture stream dirs
        # l0
        self.capture_stream_l0_dirs = self._list_dir_helper(
            trawl_dir, CAPTURE_STREAM_L0_REGEX)
        # l1
        self.capture_stream_l1_dirs = self._list_dir_helper(
            trawl_dir, CAPTURE_STREAM_L1_REGEX)

    @staticmethod
    def _list_dir_helper(trawl_dir, regex):
        """List the subdirectories matching the regex

        :param trawl_dir: string : The full path to the trawl directory.
        :param regex: string : The full path to the trawl directory.
        :return: list: full path to directories matching regex
        """
        # list all dirs in the trawl_dir
        sub_dirs = [
            d for d in os.listdir(trawl_dir) if
            os.path.isdir(os.path.join(trawl_dir, d))]
        return [os.path.join(trawl_dir, d) for d in sub_dirs if
                re.match(regex, d)]

    def prune_rdb_products(self):
        """ prune cb_dirs
        this is tested by checking if there are any cs_dirs that start with the cb.
        cb's will only be transferred once all their streams have their
        complete token set.

        :return: int: number of rdb products pruned
        """
        start_count = len(self.capture_block_dirs)
        for cb in self.capture_block_dirs[:]:
            for cs in self.capture_stream_l0_dirs:
                if cs.startswith(cb):
                    self.capture_block_dirs.remove(cb)
                    break
        for cb in self.capture_block_dirs[:]:
            for cs in self.capture_stream_l1_dirs:
                if cs.startswith(cb):
                    self.capture_block_dirs.remove(cb)
                    break
        return start_count - len(self.capture_block_dirs)

    @staticmethod
    def _get_products_factory(product_dirs, product_class):
        return [product_class(product_path) for product_path in product_dirs]

    def get_l0_products(self):
        return self._get_products_factory(
            self.capture_stream_l0_dirs, L0Product)

    def get_l1_products(self):
        return self._get_products_factory(
            self.capture_stream_l1_dirs, L1Product)

    def get_rdb_products(self):
        return self._get_products_factory(
            self.capture_block_dirs, RDBProduct)


def main(trawl_dir, boto_dict, solr_url):
    """Main loop for python script. Trawl directory and ingest products into
    archive.  Loop forever, catch any exceptions and continue.

    Parameters
    ----------
    trawl_dir: string : Full path to directory to trawl for products.
    boto_dict: dict : A boto configuration dict.
    sorl_url: string : A solr end point for metadata handeling.
    """
    # test s3 connection
    s3_conn = get_s3_connection(boto_dict)
    s3_conn.close()

    # Outer loop: Trawl forever. Catch all exceptions.
    # Inner loop: Test S3 forever. Catch S3Response error or socket error.
    #            Break back to outer loop on success.
    while True:
        try:
            ret = trawl(trawl_dir, boto_dict, solr_url)
            if ret == 0:
                # if we did not upload anything, probably a good idea to sleep for SLEEP_TIME
                time.sleep(SLEEP_TIME)
        except (socket.error, boto.exception.S3ResponseError, pysolr.SolrError):
            logger.error("Exception thrown while trawling. Test solr and s3 connection before continuing.")
            while True:
                try:
                    s3_conn = get_s3_connection(boto_dict)
                    solr_conn = pysolr.Solr(solr_url)
                    solr_conn.search('*:*')
                except Exception as e:
                    logger.debug('Caught exception.')
                    logger.debug('Exception: %s', str(e))
                    logger.debug('Sleeping for %i before continuing.', SLEEP_TIME)
                    time.sleep(SLEEP_TIME)
                else:
                    s3_conn.close()
                    break
            continue
        except Exception:
            logger.exception("Exception thrown while trawling.")
            break


def trawl(trawl_dir, boto_dict, solr_url):
    """Main action for trawling a directory for ingesting products
    into the archive.

    Parameters
    ----------
    trawl_dir: string : Full path to directory to trawl for products.
    boto_dict: dict : A boto configuration dict.
    sorl_url: string : A solr end point for metadata handeling.

    Return
    ------
    upload_size: int : The size in bytes of data uploaded. Can be used to
        wait for a set time before trawling directory again.
    """
    product_factory = ProductFactory(trawl_dir)
    total_pruned = product_factory.prune_rdb_products()
    logger.info(
        f'A total of { total_pruned } RDB products will not be transferred this '
        f'cycle, because the corresponding streams have not completed.')

    for rdb_product in product_factory.get_rdb_products():
        # check for conditions
        rdb_product.discover_trawl_files()
        if rdb_product.completed_and_transferred():
            rdb_product.cleanup()
        else:
            transfer_result = rdb_product.transfer(trawl_dir, solr_url)  # TODO: get this from the factory
            if transfer_result == 'Failed':
                break
    upload_list = []
    for l0_product in product_factory.get_l0_products():
        # check for conditions
        l0_product.discover_trawl_files()
        if l0_product.completed_and_transferred():
            l0_product.cleanup()
        elif l0_product.file_matches:
            upload_list.extend(l0_product.file_matches)

    # TODO: Refactor me. I'm just stopping here since this refactor is already
    # TODO: becoming a little too involved.
    # batch upload numpy files
    upload_list = upload_list[0:MAX_TRANSFERS]
    upload_size = sum(os.path.getsize(f)
                      for f in upload_list if os.path.isfile(f))
    if upload_size > 0:
        logger.debug("Uploading %.2f MB of data", (upload_size // 1e6))
        proc_results = parallel_upload(trawl_dir, boto_dict, upload_list)
        for pr in proc_results:
            try:
                res = pr.result()
                logger.debug("%i transfers from future.", len(res))
            except Exception as err:
                # test s3 problems, else mark as borken
                if hasattr(err, 'bucket_name'):
                    Product.set_failed_token(
                        os.path.join(trawl_dir, err.bucket_name), str(err))
    else:
        logger.debug("No data to upload (%.2f MB)", (upload_size // 1e6))
    return upload_size



def ingest_vis_product(trawl_dir, prod_id, original_refs, prod_met_extractor, solr_url):
    """Ingest a product into the archive. This includes extracting and uploading
    metadata and then moving the product into the archive.

    Parameters
    ----------
    trawl_dir: string : full path to directory to trawl for ingest product.
    prod_id: string : unique id for the product.
    original_refs : list : list of product file(s).
    product_met_extractor: class : a metadata extractor class.
    solr_url: string : sorl endpoint for metadata queries and upload.

    Returns
    -------
    met : dict : a metadata dictionary with uploaded key:value pairs.
    """
    try:
        pm_extractor = prod_met_extractor(original_refs[0])
        pm_extractor.extract_metadata()
    except Exception as err:
        bucket_name = os.path.relpath(original_refs[0], trawl_dir).split("/", 1)[0]
        err.bucket_name = bucket_name
        err.filename = original_refs[0]
        raise
    # product metadata extraction
    mh = MetaDataHandler(solr_url, pm_extractor.product_type, prod_id, prod_id)
    # Either get the product met or at least create the core meta data
    met = mh.get_prod_met(prod_id) or mh.create_core_met()
    if "CAS.ProductTransferStatus" in met and met["CAS.ProductTransferStatus"] == "RECEIVED":
        err = MetExtractorException(
            "%s marked as RECEIVED, while trying to create new product.", prod_id)
        err.bucket_name = os.path.relpath(original_refs[0], trawl_dir).split("/", 1)[0]
        raise err
    # set metadata
    met = mh.set_product_transferring(met)
    # prepend the most common path to conform to hierarchical products
    met_original_refs = list(original_refs)
    met_original_refs.insert(0, os.path.dirname(os.path.commonprefix(original_refs)))
    met = mh.add_ref_original(met, met_original_refs)
    met = mh.add_prod_met(met, pm_extractor.metadata)
    procs = parallel_upload(trawl_dir, boto_dict, original_refs)
    transfer_list = []
    for p in procs:
        for r in p.result():
            transfer_list.append(r)
    # prepend the most common path to conform to hierarchical products
    met_transfer_refs = list(transfer_list)
    met_transfer_refs.insert(0, os.path.dirname(os.path.commonprefix(transfer_list)))
    met = mh.add_ref_datastore(met, met_transfer_refs)
    met = mh.set_product_received(met)
    return met


def transfer_files(trawl_dir, boto_dict, file_list):
    """Transfer file list to s3.

    Parameters
    ----------
    trawl_dir: string : The full path to the trawl directory
    boto_dict: dict : parameter dict for boto connection.
    file_list: list : a list of full path to files to transfer.

    Returns
    -------
    transfer_list: list : a list of s3 URLs that where transfered.
    """
    s3_conn = get_s3_connection(boto_dict)
    bucket = None
    transfer_list = []
    for filename in file_list:
        bucket_name, key_name = os.path.relpath(filename, trawl_dir).split("/", 1)
        file_size = os.path.getsize(filename)
        if not bucket or bucket.name != bucket_name:
            bucket = Product.s3_create_bucket(s3_conn, bucket_name)
        key = bucket.new_key(key_name)
        res = key.set_contents_from_filename(filename)
        if res == file_size:
            os.unlink(filename)
            transfer_list.append("/".join(["s3:/", bucket.name, key.name]))
        else:
            logger.error("%s not deleted. Only uploaded %i of %i bytes.", filename, res, file_size)
    return transfer_list


def parallel_upload(trawl_dir, boto_dict, file_list):
    """
    """
    max_workers = CPU_MULTIPLIER * multiprocessing.cpu_count()
    workers = min(len(file_list), max_workers)
    logger.debug("Using %i workers", workers)
    files = [file_list[i::workers] for i in range(workers)]
    logger.debug("Processing %i files", len(file_list))
    procs = []
    with futures.ProcessPoolExecutor(max_workers=workers) as executor:
        for f in files:
            procs.append(executor.submit(transfer_files, trawl_dir, boto_dict, f))
        executor.shutdown(wait=True)
    return procs


def s3_create_anon_access_policy(bucket_name):
    """Create a bucket policy for anonymous read access and anonymous bucket listing.
    Returns
    -------
    anon_access_policy: A json formatted s3 bucket policy
    """
    anon_policy_dict = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AddPerm",
                "Effect": "Allow",
                "Principal": "*",
                "Action": ["s3:GetObject"],
                "Resource": ["arn:aws:s3:::%s/*" % bucket_name]
            },
            {
                 "Sid": "AddPerm",
                 "Effect": "Allow",
                 "Principal": "*",
                 "Action": ["s3:ListBucket"],
                 "Resource": ["arn:aws:s3:::%s" % bucket_name]
            }
        ]
    }
    anon_access_policy = json.dumps(anon_policy_dict)
    return anon_access_policy



if __name__ == "__main__":
    katsdpservices.setup_logging()
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("katsdpvistrawler")
    katsdpservices.setup_restart()

    parser = OptionParser(usage="vis_trawler.py <trawl_directory>")
    parser.add_option("--s3-host", default="localhost",
                      help="S3 gateway host address [default = %default]")
    parser.add_option("--s3-port", type="int", default=7480,
                      help="S3 gateway port [default = %default]")
    parser.add_option("--solr-url", default="http://kat-archive.kat.ac.za:8983/solr/kat_core",
                      help="Solr end point for metadata extraction [default = %default]")

    (options, args) = parser.parse_args()
    if len(args) < 1 or not os.path.isdir(args[0]):
        print(__doc__)
        sys.exit()

    boto_dict = make_boto_dict(options)
    main(trawl_dir=args[0], boto_dict=boto_dict, solr_url=options.solr_url)
