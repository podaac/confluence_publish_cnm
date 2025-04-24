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

    granules_dict = get_granules_dict(bucket, bucket_path)
    logging.info("Retrieved granules from S3.")
    locate_efs_granules(granules_dict)
    logging.info("Located matching granules on EFS.")

    for continent, granules in granules_dict.items():
        logging.info("Collecting granule data and publishing message for %s.", continent.upper())

        granule_files = retrieve_metadata(granules["priors"], granules["results"], bucket, bucket_path)
        logging.info("Located metadata for priors/results granule.")

        message = create_message(granule_files)
        logging.info("Created CNM for priors/results granule.")
        logging.info("Message: %s", message)

        publish_cnm(message, prefix)

    end = datetime.datetime.now(datetime.timezone.utc)
    logging.info("Execution time: %s", end - start)

def create_args():
    """Create and return argparser with arguments."""
    arg_parser = argparse.ArgumentParser(description="Retrieve a list of S3 URIs")
    arg_parser.add_argument("-b",
                            "--bucket",
                            type=str,
                            help="Full path to SoS granules in S3, e.g. confluence-ops-sos")
    arg_parser.add_argument("-p",
                            "--bucketpath",
                            type=str,
                            help="Full path to SoS granules in S3, e.g. unconstrained/0001")
    arg_parser.add_argument("-r",
                            "--prefix",
                            type=str,
                            help="Prefix for venue resources, e.g. svc-confluence-sit")
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

def locate_efs_granules(granules_dict):
    """Locate SoS granules on EFS and update granule dictionary."""
    for continent, granules in granules_dict.items():
        priors_file = DATA_PRIORS.joinpath(granules["priors"])
        results_file = DATA_RESULTS.joinpath(granules["results"])
        if priors_file.exists() and results_file.exists():
            granules_dict[continent]["priors"] = priors_file
            granules_dict[continent]["results"] = results_file
        else:
            logging.error("Could not locate granule pair: %s and %s", priors_file, results_file)
            raise FileNotFoundError(f"Could not locate granule pair: {priors_file} and {results_file}")

def retrieve_metadata(priors_file, results_file, bucket, bucket_path):
    """Retrieve metadata for each file in the granule dictionary."""
    priors_s3, results_s3 = rename_s3_files(priors_file, results_file, bucket, bucket_path)
    return [
        {
            "name": priors_s3,
            "type": "data",
            "uri": f"s3://{bucket}/{bucket_path}/{priors_s3}",
            "size": os.stat(priors_file).st_size,
            "checksum": get_checksum(priors_file),
            "checksumType": "md5"
        },
        {
            "name": results_s3,
            "type": "data",
            "uri": f"s3://{bucket}/{bucket_path}/{results_s3}",
            "size": os.stat(results_file).st_size,
            "checksum": get_checksum(results_file),
            "checksumType": "md5"
        }
    ]

def rename_s3_files(priors_file, results_file, bucket, bucket_path):
    """Rename granules to include run type, version, and run time."""

    run_type = bucket_path.split("/")[0]
    version = bucket_path.split("/")[-1]
    run_time = get_runtime(priors_file)
    updated_priors = f"{priors_file.name.split('_priors.nc')[0]}_{run_type}_{version}_{run_time}_priors.nc"
    updated_results = f"{results_file.name.split('_results.nc')[0]}_{run_type}_{version}_{run_time}_results.nc"
    S3.upload_file(priors_file, bucket, f"{bucket_path}/{updated_priors}")
    logging.info("Uploaded: s3://%s/%s/%s", bucket, bucket_path, updated_priors)
    S3.upload_file(results_file, bucket, f"{bucket_path}/{updated_results}")
    logging.info("Uploaded: s3://%s/%s/%s", bucket, bucket_path, updated_results)

    # S3.delete_object(Bucket=bucket, Key=f"{bucket_path}/{priors_file.name}")
    # logging.info("Deleted: s3://%s/%s/%s", bucket, bucket_path, priors_file.name)
    # S3.delete_object(Bucket=bucket, Key=f"{bucket_path}/{results_file.name}")
    # logging.info("Deleted: s3://%s/%s/%s", bucket, bucket_path, results_file.name)

    return updated_priors, updated_results

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