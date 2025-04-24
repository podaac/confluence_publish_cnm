# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "cw_log_group_publish_cnm" {
  name              = "/aws/batch/job/${var.prefix}-publish-cnm/"
  retention_in_days = 0
}

# SSM parameter
resource "aws_ssm_parameter" "publish_cnm_sns_topic" {
  name        = "${var.prefix}-podaac-cnm-topic-arn"
  description = "Cumulus SNS topic for granule ingestion"
  type        = "SecureString"
  value       = var.sns_topic_arn
}

# Job Queue
resource "aws_batch_job_queue" "jq_publish_cnm" {
  name = "${var.prefix}-publish-cnm"
  state = "ENABLED"
  priority = 10
  compute_environment_order {
    order = 1
    compute_environment = data.aws_batch_compute_environment.ce_data.arn
  }
}

# S3 bucket policy
resource "aws_s3_bucket_policy" "allow_cross_account_access" {
  bucket = data.aws_s3_bucket.aws_s3_bucket_sos
  policy = aws_iam_policy.s3_sos_bucket_policy
}

resource "aws_iam_policy" "s3_sos_bucket_policy" {
  name = "${var.prefix}-cnm-publish-sos-bucket-policy"
  policy = jsonencode({
    "Version": "2012-10-17",
    "Statement": [
        {
          "Sid": "ListBucketGetObject",
          "Effect": "Allow",
          "Principal": {
              "AWS": "arn:aws:iam::${var.cross_account}:root"
          },
          "Action": [
              "s3:ListBucket",
              "s3:GetObject",
              "s3:GetObjectAttributes",
          ],
          "Resource": [
              "${data.aws_s3_bucket.aws_s3_bucket_sos.arn}"
          ]
        }
    ]
  })
}

# Job Role
resource "aws_iam_role" "batch_job_role" {
  name        = "${var.prefix}-batch-job-role-publish-cnm"
  description = "Amazon Batch job role for publish cnm"
  assume_role_policy = jsonencode({
    "Version" : "2012-10-17",
    "Statement" : [
      {
        "Effect" : "Allow",
        "Principal" : {
          "Service" : "ecs-tasks.amazonaws.com"
        },
        "Action" : "sts:AssumeRole"
      }
    ]
  })
}

# # S3
resource "aws_iam_role_policy_attachment" "batch_job_s3_role_policy" {
  role       = aws_iam_role.batch_job_role.name
  policy_arn = aws_iam_policy.batch_job_s3_policy.arn
}

resource "aws_iam_policy" "batch_job_s3_policy" {
  name        = "${var.prefix}-batch-job-publish-cnm-s3-policy"
  description = "Amazon Batch job policy for S3 actions"
  policy = jsonencode({
    "Version" : "2012-10-17",
    "Statement" : [
      {
        "Sid" : "AllowListAllBuckets",
        "Effect" : "Allow",
        "Action" : "s3:ListAllMyBuckets",
        "Resource" : "*"
      },
      {
        "Sid" : "AllowListBuckets",
        "Effect" : "Allow",
        "Action" : [
          "s3:ListBucket",
          "s3:ListBucketVersions"
        ],
        "Resource" : [
          "${data.aws_s3_bucket.aws_s3_bucket_sos.arn}"
        ]
      },
      {
        "Sid" : "AllGetPutObjects",
        "Effect" : "Allow",
        "Action" : [
          "s3:DeleteObject",
          "s3:GetObject",
          "s3:GetObjectAttributes",
          "s3:ListMultipartUploadParts",
          "s3:PutObject"
        ],
        "Resource" : [
          "${data.aws_s3_bucket.aws_s3_bucket_sos.arn}/*"
        ]
      }
    ]
  })
}

# # SNS
resource "aws_iam_role_policy_attachment" "batch_job_sns_role_policy" {
  role       = aws_iam_role.batch_job_role.name
  policy_arn = aws_iam_policy.batch_job_sns_policy.arn
}

resource "aws_iam_policy" "batch_job_sns_policy" {
  name        = "${var.prefix}-batch-job-publish-cnm-sns-policy"
  description = "Amazon Batch job policy to access SNS topics"
  policy = jsonencode({
    "Version" : "2012-10-17",
    "Statement" : [
      {
        "Sid" : "AllowPublish",
        "Effect" : "Allow",
        "Action" : "sns:Publish",
        "Resource" : "${var.sns_topic_arn}"
      }
    ]
  })
}

# # SSM
resource "aws_iam_role_policy_attachment" "batch_job_ssm_role_policy" {
  role       = aws_iam_role.batch_job_role.name
  policy_arn = aws_iam_policy.batch_job_ssm_policy.arn
}

resource "aws_iam_policy" "batch_job_ssm_policy" {
  name        = "${var.prefix}-batch-job-publish-cnm-ssm-policy"
  description = "Amazon Batch job policy to access SSM parameters"
  policy = jsonencode({
    "Version" : "2012-10-17",
    "Statement" : [
      {
        "Sid" : "AllowGetParameter",
        "Effect" : "Allow",
        "Action" : [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath"
        ],
        "Resource" : "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter/${var.prefix}-podaac-cnm-topic-arn"
      }
    ]
  })
}

# Job Definition
resource "aws_batch_job_definition" "generate_batch_jd_publish_cnm" {
  name                  = "${var.prefix}-publish-cnm"
  type                  = "container"
  container_properties  = <<CONTAINER_PROPERTIES
  {
    "image": "${local.account_id}.dkr.ecr.us-west-2.amazonaws.com/${var.prefix}-publish-cnm",
    "executionRoleArn": "${data.aws_iam_role.exe_role.arn}",
    "jobRoleArn": "${aws_iam_role.batch_job_role.arn}",
    "fargatePlatformConfiguration": { "platformVersion": "LATEST" },
    "logConfiguration": {
      "logDriver" : "awslogs",
      "options": {
        "awslogs-group" : "${aws_cloudwatch_log_group.cw_log_group_publish_cnm.name}"
      }
    },
    "environment": [
      {
        "name": "COLLECTION",
        "value": "${var.collection}"
      },
      {
        "name": "PROVIDER",
        "value": "${var.data_provider}"
      },
      {
        "name": "VERSION",
        "value": "${var.provider_version}"
      }
    ],
    "resourceRequirements": [
      {"type": "MEMORY", "value": "8192"},
      {"type": "VCPU", "value": "4"}
    ],
    "mountPoints": [
      {
        "sourceVolume": "input",
        "containerPath": "/mnt/input",
        "readOnly": false
      },
      {
        "sourceVolume": "output",
        "containerPath": "/mnt/output",
        "readOnly": false
      }
    ],
    "volumes": [
      {
        "name": "input",
        "efsVolumeConfiguration": {
          "fileSystemId": "${data.aws_efs_file_system.aws_efs_input.file_system_id}",
          "rootDirectory": "/"
        }
      },
      {
        "name": "output",
        "efsVolumeConfiguration": {
          "fileSystemId": "${data.aws_efs_file_system.aws_efs_output.file_system_id}",
          "rootDirectory": "/"
        }
      }
    ]
  }
  CONTAINER_PROPERTIES
  platform_capabilities = ["FARGATE"]
  propagate_tags        = true
  tags                  = { "job_definition" : "${var.prefix}-publish-cnm" }
}
