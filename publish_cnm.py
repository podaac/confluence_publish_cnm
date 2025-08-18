"""AWS Lambda that publishes a CNM for each granule in an S3 bucket and triggers
ingestion.

Calcuates checksum and file size and creates a message for each prior/results
continent pair.
"""

# Standard imports
import argparse
import datetime
import hashlib
import json
import logging
import os
import pathlib

# Third-party imports
import boto3
import botocore
from netCDF4 import Dataset

# Constants
COLLECTION = os.environ["COLLECTION"]
DATA_PRIORS = pathlib.Path("/mnt/input/sos")
DATA_RESULTS = pathlib.Path("/mnt/output/sos")
PROVIDER = os.environ["PROVIDER"]
S3 = boto3.client("s3")
SNS = boto3.client("sns")
SSM = boto3.client("ssm")
VERSION = os.environ["VERSION"]

logging.getLogger().setLevel(logging.INFO)
logging.basicConfig(format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                    datefmt='%Y-%m-%dT%H:%M:%S',
                    level=logging.INFO)

def main():
    start = datetime.datetime.now(datetime.timezone.utc)

    arg_parser = create_args()
    args = arg_parser.parse_args()
    bucket = args.bucket
    logging.info("S3 bucket: %s", bucket)
    bucket_path = args.bucketpath
    logging.info("S3 bucket path to granules: %s", bucket_path)
    prefix = args.prefix
    logging.info("Venue prefix: %s", prefix)
    podaac_bucket = args.podaacbucket
    logging.info("PO.DAAC S3 bucket: %s", podaac_bucket)
    test = args.test
    logging.info("Test run: %s", test)

    granules_dict = get_granules_dict(bucket, bucket_path)
    logging.info("Retrieved granules from S3.")
    locate_efs_granules(granules_dict, bucket, bucket_path)
    logging.info("Located matching granules on EFS.")

    for continent, granules in granules_dict.items():
        logging.info("Collecting granule data and publishing message for %s.", continent.upper())

        granule_files = retrieve_metadata(granules["priors"], granules["results"], podaac_bucket, bucket_path, prefix)
        logging.info("Located metadata for priors/results granule.")

        message = create_message(granule_files)
        logging.info("Created CNM for priors/results granule.")
        logging.info("Message: %s", message)

        if not test:
            publish_cnm(message, prefix)
        else:
            logging.info("Test run so CNM message was not published.")

    end = datetime.datetime.now(datetime.timezone.utc)
    logging.info("Execution time: %s", end - start)

def create_args():
    """Create and return argparser with arguments."""
    arg_parser = argparse.ArgumentParser(description="Retrieve a list of S3 URIs")
    arg_parser.add_argument("-b",
                            "--bucket",
                            type=str,
                            help="S3 bucket that contains SoS granules, e.g. confluence-ops-sos")
    arg_parser.add_argument("-p",
                            "--bucketpath",
                            type=str,
                            help="Full path to SoS granules in S3, e.g. unconstrained/0001")
    arg_parser.add_argument("-r",
                            "--prefix",
                            type=str,
                            help="Prefix for venue resources, e.g. svc-confluence-sit")
    arg_parser.add_argument("-s",
                            "--podaacbucket",
                            type=str,
                            help="S3 bucket to upload granules for ingestion to, e.g. podaac-dev-swot-sos")
    arg_parser.add_argument("-t",
                            "--test",
                            action="store_true",
                            help="Indicates this is a test run and granules should not be ingested")
    return arg_parser

def get_granules_dict(bucket, bucket_path):
    """Return a dictionary organized by continent of SoS granules."""
    paginator = S3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=bucket_path)
    granule_list = []
    for page in pages:
        for obj in page["Contents"]:
            obj_name = obj["Key"].split("/")[-1]
            if "unconstrained" not in obj_name and "constrained" not in obj_name:
                granule_list.append(obj_name)

    continents = list(set([ granule.split('_')[0] for granule in granule_list ]))
    granule_dict = {}
    for continent in continents:
        granule_dict[continent] = {}
        for granule in granule_list:
            if "priors" in granule and continent in granule:
                granule_dict[continent]["priors"] = granule
            if "results" in granule and continent in granule:
                granule_dict[continent]["results"] = granule
    return granule_dict

def locate_efs_granules(granules_dict, bucket, bucket_path):
    """Locate SoS granules on EFS and update granule dictionary."""
    for continent, granules in granules_dict.items():
        priors_file = DATA_PRIORS.joinpath(granules["priors"])
        results_file = DATA_RESULTS.joinpath(granules["results"])
        if not priors_file.exists() or not results_file.exists():
            logging.error("Could not locate granule pair: %s and %s. Attempting to download from S3...", priors_file, results_file)
            download_from_s3(priors_file, results_file, bucket, bucket_path)
            if not priors_file.exists() or not results_file.exists():
                raise FileNotFoundError(f"Could not locate granule pair: {priors_file} and {results_file}")
        granules_dict[continent]["priors"] = priors_file
        granules_dict[continent]["results"] = results_file

def download_from_s3(priors_file, results_file, bucket, bucket_path):
    """Download priors and/or results file from S3 if needed."""

    if not priors_file.exists():
        S3.download_file(
            bucket,
            f"{bucket_path}/{priors_file.name}",
            priors_file
        )
        logging.error("Downloaded from S3: %s.", priors_file)
    if not results_file.exists():
        S3.download_file(
            bucket,
            f"{bucket_path}/{results_file.name}",
            results_file
        )
        logging.error("Downloaded from S3: %s.", results_file)

def retrieve_metadata(priors_file, results_file, podaac_bucket, bucket_path, prefix):
    """Retrieve metadata for each file in the granule dictionary."""
    priors_s3, results_s3 = rename_s3_files(priors_file, results_file, podaac_bucket, bucket_path, prefix)
    return [
        {
            "name": priors_s3,
            "type": "data",
            "uri": f"s3://{podaac_bucket}/{COLLECTION}/{priors_s3}",
            "size": os.stat(priors_file).st_size,
            "checksum": get_checksum(priors_file),
            "checksumType": "md5"
        },
        {
            "name": results_s3,
            "type": "data",
            "uri": f"s3://{podaac_bucket}/{COLLECTION}/{results_s3}",
            "size": os.stat(results_file).st_size,
            "checksum": get_checksum(results_file),
            "checksumType": "md5"
        }
    ]

def rename_s3_files(priors_file, results_file, podaac_bucket, bucket_path, prefix):
    """Rename granules to include run type, version, and run time."""

    # creds = get_podaac_creds(prefix)
    # s3_podaac = boto3.client(
    #     "s3",
    #     aws_access_key_id=creds["access_key"],
    #     aws_secret_access_key=creds["secret"]
    # )

    run_type = bucket_path.split("/")[0]
    version = bucket_path.split("/")[-1]
    run_time = get_runtime(priors_file)
    updated_priors = f"{priors_file.name.split('_priors.nc')[0]}_{run_type}_{version}_{run_time}_priors.nc"
    updated_results = f"{results_file.name.split('_results.nc')[0]}_{run_type}_{version}_{run_time}_results.nc"
    # s3_podaac.upload_file(priors_file, podaac_bucket, f"{COLLECTION}/{updated_priors}")
    # logging.info("Uploaded: s3://%s/%s/%s", podaac_bucket, COLLECTION, updated_priors)
    # s3_podaac.upload_file(results_file, podaac_bucket, f"{COLLECTION}/{updated_results}")
    # logging.info("Uploaded: s3://%s/%s/%s", podaac_bucket, COLLECTION, updated_results)

    return updated_priors, updated_results

def get_podaac_creds(prefix):
    """Return PO.DAAC S3 credentials stored in SSM Parameter Store."""

    creds = {}
    try:
        creds["access_key"] = SSM.get_parameter(Name=f"{prefix}-podaac-key", WithDecryption=True)["Parameter"]["Value"]
        creds["secret"] = SSM.get_parameter(Name=f"{prefix}-podaac-secret", WithDecryption=True)["Parameter"]["Value"]
    except botocore.exceptions.ClientError as e:
        raise e
    return creds

def get_runtime(file_name):
    """Get runtime timestamp from global attributes of SoS file."""

    sos_ds = Dataset(file_name, 'r')
    runtime = sos_ds.date_modified
    sos_ds.close()

    runtime_ds = datetime.datetime.strptime(runtime, '%Y-%m-%dT%H:%M:%S').strftime('%Y%m%dT%H%M%S')
    return runtime_ds

def get_checksum(file_path, block_size=2**20):
    """Return checksum for file contents."""
    checksum = hashlib.md5()
    with open(file_path, "rb") as f:
        while True:
            buffer = f.read(block_size)
            if not buffer:
                break
            checksum.update(buffer)
    return checksum.hexdigest()

def create_message(granule_files):
    """Create CNM for granule."""
    identifier = granule_files[0]["name"].split('_')[:-1]
    identifier = "_".join(identifier)
    message = {
        "version": VERSION,
        "provider": PROVIDER,
        "collection": COLLECTION,
        "submissionTime": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "identifier": identifier,
        "product": {
            "name": identifier,
            "files": granule_files,
            "dataVersion": VERSION
        }
    }
    return message

def publish_cnm(message, prefix):
    """Publish CNM message to SNS Topic."""
    topic_arn = SSM.get_parameter(Name=f"{prefix}-podaac-cnm-topic-arn", WithDecryption=True)["Parameter"]["Value"]
    SNS.publish(TopicArn=topic_arn, Message=json.dumps(message),
    )
    logging.info(f"{message['identifier']} message published to SNS Topic: {topic_arn}")

if __name__ == "__main__":
    main()