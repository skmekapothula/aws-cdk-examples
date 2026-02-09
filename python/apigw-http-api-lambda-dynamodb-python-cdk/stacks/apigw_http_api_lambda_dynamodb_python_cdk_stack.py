# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_dynamodb as dynamodb_,
    aws_lambda as lambda_,
    aws_apigateway as apigw_,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_cloudwatch as cloudwatch_,
    aws_logs as logs_,
    aws_s3 as s3_,
    Duration,
)
from constructs import Construct

TABLE_NAME = "demo_table"


class ApigwHttpApiLambdaDynamodbPythonCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Centralized log bucket for long-term storage
        log_bucket = s3_.Bucket(
            self,
            "CentralizedLogBucket",
            encryption=s3_.BucketEncryption.S3_MANAGED,
            block_public_access=s3_.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[
                s3_.LifecycleRule(
                    transitions=[
                        s3_.Transition(
                            storage_class=s3_.StorageClass.INTELLIGENT_TIERING,
                            transition_after=Duration.days(90),
                        ),
                        s3_.Transition(
                            storage_class=s3_.StorageClass.GLACIER,
                            transition_after=Duration.days(365),
                        ),
                    ],
                    expiration=Duration.days(2555),  # 7 years
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # VPC
        vpc = ec2.Vpc(
            self,
            "Ingress",
            cidr="10.1.0.0/16",
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Private-Subnet", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24
                )
            ],
        )

        # VPC Flow Logs
        vpc_flow_log_group = logs_.LogGroup(
            self,
            "VpcFlowLogGroup",
            retention=logs_.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.DESTROY,
        )

        vpc.add_flow_log(
            "VpcFlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(vpc_flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )
        
        # Create VPC endpoint
        dynamo_db_endpoint = ec2.GatewayVpcEndpoint(
            self,
            "DynamoDBVpce",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            vpc=vpc,
        )

        # This allows to customize the endpoint policy
        dynamo_db_endpoint.add_to_policy(
            iam.PolicyStatement(  # Restrict to listing and describing tables
                principals=[iam.AnyPrincipal()],
                actions=[                "dynamodb:DescribeStream",
                "dynamodb:DescribeTable",
                "dynamodb:Get*",
                "dynamodb:Query",
                "dynamodb:Scan",
                "dynamodb:CreateTable",
                "dynamodb:Delete*",
                "dynamodb:Update*",
                "dynamodb:PutItem"],
                resources=["*"],
            )
        )

        # Create DynamoDb Table with point-in-time recovery
        demo_table = dynamodb_.Table(
            self,
            TABLE_NAME,
            partition_key=dynamodb_.Attribute(
                name="id", type=dynamodb_.AttributeType.STRING
            ),
            point_in_time_recovery=True,
            stream=dynamodb_.StreamViewType.NEW_AND_OLD_IMAGES,
        )

        # Create the Lambda function with log retention
        api_hanlder = lambda_.Function(
            self,
            "ApiHandler",
            function_name="apigw_handler",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("lambda/apigw-handler"),
            handler="index.handler",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            memory_size=1024,
            timeout=Duration.minutes(5),
            tracing=lambda_.Tracing.ACTIVE,
            log_retention=logs_.RetentionDays.ONE_YEAR,
        )

        # grant permission to lambda to write to demo table
        demo_table.grant_write_data(api_hanlder)
        api_hanlder.add_environment("TABLE_NAME", demo_table.table_name)

        # API Gateway access logs
        api_log_group = logs_.LogGroup(
            self,
            "ApiGatewayAccessLogs",
            retention=logs_.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create API Gateway with X-Ray tracing and access logging
        api = apigw_.LambdaRestApi(
            self,
            "Endpoint",
            handler=api_hanlder,
            deploy_options=apigw_.StageOptions(
                tracing_enabled=True,
                access_log_destination=apigw_.LogGroupLogDestination(api_log_group),
                access_log_format=apigw_.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
            ),
        )

        # CloudWatch Alarms for monitoring
        cloudwatch_.Alarm(
            self,
            "LambdaErrorAlarm",
            metric=api_hanlder.metric_errors(),
            threshold=1,
            evaluation_periods=1,
            alarm_description="Alert when Lambda function errors occur",
        )

        cloudwatch_.Alarm(
            self,
            "ApiGateway5xxAlarm",
            metric=api.metric_server_error(),
            threshold=5,
            evaluation_periods=1,
            alarm_description="Alert when API Gateway returns 5xx errors",
        )

        cloudwatch_.Alarm(
            self,
            "ApiGateway4xxAlarm",
            metric=api.metric_client_error(),
            threshold=10,
            evaluation_periods=1,
            alarm_description="Alert when API Gateway returns excessive 4xx errors",
        )
