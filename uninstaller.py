#!/usr/bin/env python3
"""
AWS Infrastructure Uninstaller
This script deletes all AWS infrastructure resources created by installer.py.
"""

import boto3
import time
import logging
from botocore.exceptions import ClientError

# Configuration (must match installer.py)
project_name = "strands-ecs" # at least 3 characters
region = "us-west-2"

sts_client = boto3.client("sts", region_name=region)
account_id = sts_client.get_caller_identity()["Account"]

# Initialize boto3 clients
s3_client = boto3.client("s3", region_name=region)
iam_client = boto3.client("iam", region_name=region)
secrets_client = boto3.client("secretsmanager", region_name=region)
opensearch_client = boto3.client("opensearchserverless", region_name=region)
ec2_client = boto3.client("ec2", region_name=region)
elbv2_client = boto3.client("elbv2", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)
s3vectors_client = boto3.client("s3vectors", region_name=region)
ecs_client = boto3.client("ecs", region_name=region)
ecr_client = boto3.client("ecr", region_name=region)
logs_client = boto3.client("logs", region_name=region)

# Get account ID if not set
if not account_id:
    account_id = sts_client.get_caller_identity()["Account"]

bucket_name = f"storage-for-{project_name}-{account_id}-{region}"
vector_index_name = project_name
vector_bucket_name = f"{project_name}-{account_id}"
cloudfront_comment_marker = f"CloudFront-for-{project_name}"
cloudfront_oai_comment_marker = f"OAI for {project_name} S3 bucket"

# Configure logging
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger(__name__)

logger = setup_logging()

def list_project_cloudfront_distributions():
    """List CloudFront distributions created by installer.py."""
    distributions = []
    paginator = cloudfront_client.get_paginator("list_distributions")
    for page in paginator.paginate():
        for dist in page.get("DistributionList", {}).get("Items", []) or []:
            if cloudfront_comment_marker in dist.get("Comment", ""):
                distributions.append(dist)
    return distributions


def get_cloudfront_distribution_state(dist_id):
    """Return deployment status and enabled flag for a distribution."""
    dist_response = cloudfront_client.get_distribution(Id=dist_id)
    config_response = cloudfront_client.get_distribution_config(Id=dist_id)
    return {
        "status": dist_response["Distribution"]["Status"],
        "enabled": config_response["DistributionConfig"]["Enabled"],
        "etag": config_response["ETag"],
    }


def wait_for_cloudfront_distributions_disabled(dist_ids, timeout_seconds=1500, poll_interval=30):
    """Wait until all distributions are disabled and fully deployed."""
    if not dist_ids:
        return True

    logger.info(
        f"  Waiting for CloudFront disable deployment (up to {timeout_seconds // 60} minutes)..."
    )
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pending = []
        for dist_id in dist_ids:
            try:
                state = get_cloudfront_distribution_state(dist_id)
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchDistribution":
                    continue
                raise

            if state["enabled"] or state["status"] != "Deployed":
                pending.append(f"{dist_id}({state['status']}, enabled={state['enabled']})")

        if not pending:
            logger.info("  ✓ All CloudFront distributions are disabled and deployed")
            return True

        logger.info(f"  Still waiting: {', '.join(pending)}")
        time.sleep(poll_interval)

    logger.error("  ✗ Timed out waiting for CloudFront distributions to finish disabling")
    return False


def delete_cloudfront_origin_access_identities():
    """Delete Origin Access Identities created for the project S3 bucket."""
    try:
        oai_list = cloudfront_client.list_cloud_front_origin_access_identities()
        for oai in oai_list.get("CloudFrontOriginAccessIdentityList", {}).get("Items", []) or []:
            if cloudfront_oai_comment_marker not in oai.get("Comment", ""):
                continue

            oai_id = oai["Id"]
            try:
                cloudfront_client.delete_cloud_front_origin_access_identity(Id=oai_id)
                logger.info(f"  ✓ Deleted Origin Access Identity: {oai_id}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "CloudFrontOriginAccessIdentityInUse":
                    logger.warning(
                        f"  OAI {oai_id} is still in use by another distribution; delete manually if needed"
                    )
                elif e.response["Error"]["Code"] != "NoSuchCloudFrontOriginAccessIdentity":
                    logger.warning(f"  Could not delete OAI {oai_id}: {e}")
    except Exception as e:
        logger.warning(f"  Could not delete Origin Access Identities: {e}")


def delete_cloudfront_resources():
    """Disable, wait for deployment, delete CloudFront distributions and related OAI."""
    logger.info("[1/10] Deleting CloudFront resources")

    try:
        distributions = list_project_cloudfront_distributions()
        if not distributions:
            logger.info(f"  No CloudFront distributions found for comment: {cloudfront_comment_marker}")
            delete_cloudfront_origin_access_identities()
            logger.info("✓ CloudFront resources processed")
            return True

        dist_ids = [dist["Id"] for dist in distributions]
        logger.info(f"  Found CloudFront distribution(s): {dist_ids}")

        for dist in distributions:
            dist_id = dist["Id"]
            state = get_cloudfront_distribution_state(dist_id)
            if not state["enabled"]:
                logger.info(f"  Distribution {dist_id} is already disabled")
                continue

            config_response = cloudfront_client.get_distribution_config(Id=dist_id)
            config = config_response["DistributionConfig"]
            config["Enabled"] = False
            cloudfront_client.update_distribution(
                Id=dist_id,
                DistributionConfig=config,
                IfMatch=config_response["ETag"],
            )
            logger.info(f"  Disabled distribution: {dist_id}")

        if not wait_for_cloudfront_distributions_disabled(dist_ids):
            logger.error("  CloudFront distributions were not fully disabled; skipping deletion")
            return False

        remaining = []
        for dist_id in dist_ids:
            try:
                state = get_cloudfront_distribution_state(dist_id)
                cloudfront_client.delete_distribution(Id=dist_id, IfMatch=state["etag"])
                logger.info(f"  ✓ Deleted distribution: {dist_id}")
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "NoSuchDistribution":
                    logger.debug(f"  Distribution {dist_id} already deleted")
                else:
                    remaining.append(dist_id)
                    logger.warning(f"  Could not delete distribution {dist_id}: {e}")

        delete_cloudfront_origin_access_identities()

        if remaining:
            logger.error(f"  ✗ CloudFront distribution(s) still remain: {remaining}")
            return False

        logger.info("✓ CloudFront resources deleted")
        return True
    except Exception as e:
        logger.error(f"Error deleting CloudFront resources: {e}")
        return False

def delete_alb_resources():
    """Delete ALB, target groups, and listeners."""
    logger.info("[2/9] Deleting ALB resources")
    
    try:
        # Delete ALB and its listeners first
        alb_name = f"alb-for-{project_name}"
        try:
            albs = elbv2_client.describe_load_balancers(Names=[alb_name])
            if albs["LoadBalancers"]:
                alb_arn = albs["LoadBalancers"][0]["LoadBalancerArn"]
                
                # Delete listeners first
                listeners = elbv2_client.describe_listeners(LoadBalancerArn=alb_arn)
                for listener in listeners["Listeners"]:
                    elbv2_client.delete_listener(ListenerArn=listener["ListenerArn"])
                    logger.info(f"  ✓ Deleted listener: {listener['ListenerArn']}")
                
                # Delete ALB
                elbv2_client.delete_load_balancer(LoadBalancerArn=alb_arn)
                logger.info(f"  ✓ Deleted ALB: {alb_name}")
                
                # Wait for ALB to be deleted
                time.sleep(30)
        except ClientError as e:
            if e.response["Error"]["Code"] != "LoadBalancerNotFound":
                raise
        
        # Delete target groups after ALB is deleted
        tgs = elbv2_client.describe_target_groups()
        for tg in tgs["TargetGroups"]:
            if f"TG-for-{project_name}" in tg["TargetGroupName"]:
                try:
                    elbv2_client.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
                    logger.info(f"  ✓ Deleted target group: {tg['TargetGroupName']}")
                except ClientError as e:
                    if e.response["Error"]["Code"] != "ResourceInUse":
                        logger.warning(f"  Could not delete target group {tg['TargetGroupName']}: {e}")
        
        logger.info("✓ ALB resources deleted")
    except Exception as e:
        logger.error(f"Error deleting ALB resources: {e}")

def delete_ecs_resources():
    """Delete ECS cluster, service, task definitions, log group, and ECR repository."""
    logger.info("[3/9] Deleting ECS resources")

    cluster_name = f"cluster-for-{project_name}"
    service_name = f"service-for-{project_name}"
    log_group_name = f"/ecs/app-for-{project_name}"
    repository_name = f"ecr-for-{project_name}"
    task_family = f"task-for-{project_name}"

    try:
        services = ecs_client.describe_services(cluster=cluster_name, services=[service_name])
        if services["services"] and services["services"][0]["status"] != "INACTIVE":
            ecs_client.update_service(cluster=cluster_name, service=service_name, desiredCount=0)
            logger.info(f"  ✓ Scaled ECS service to 0: {service_name}")
            time.sleep(10)
            ecs_client.delete_service(cluster=cluster_name, service=service_name, force=True)
            logger.info(f"  ✓ Deleted ECS service: {service_name}")
            time.sleep(15)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ["ClusterNotFoundException", "ServiceNotFoundException"]:
            logger.warning(f"  Could not delete ECS service: {e}")

    try:
        ecs_client.delete_cluster(cluster=cluster_name)
        logger.info(f"  ✓ Deleted ECS cluster: {cluster_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ClusterNotFoundException":
            logger.warning(f"  Could not delete ECS cluster: {e}")

    try:
        task_defs = ecs_client.list_task_definitions(familyPrefix=task_family, sort="DESC")
        for task_def_arn in task_defs.get("taskDefinitionArns", []):
            ecs_client.deregister_task_definition(taskDefinition=task_def_arn)
            logger.info(f"  ✓ Deregistered task definition: {task_def_arn}")
    except ClientError as e:
        logger.warning(f"  Could not deregister task definitions: {e}")

    try:
        logs_client.delete_log_group(logGroupName=log_group_name)
        logger.info(f"  ✓ Deleted log group: {log_group_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            logger.warning(f"  Could not delete log group: {e}")

    try:
        ecr_client.delete_repository(repositoryName=repository_name, force=True)
        logger.info(f"  ✓ Deleted ECR repository: {repository_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            logger.warning(f"  Could not delete ECR repository: {e}")

    logger.info("✓ ECS resources deleted")

def delete_ec2_instances():
    """Delete EC2 instances."""
    logger.info("[3/9] Deleting EC2 instances")
    
    try:
        instances = ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [f"app-for-{project_name}"]},
                {"Name": "instance-state-name", "Values": ["running", "pending", "stopping", "stopped"]}
            ]
        )
        
        instance_ids = []
        for reservation in instances["Reservations"]:
            for instance in reservation["Instances"]:
                instance_ids.append(instance["InstanceId"])
        
        if instance_ids:
            ec2_client.terminate_instances(InstanceIds=instance_ids)
            logger.info(f"  ✓ Terminated instances: {instance_ids}")
            
            # Wait for termination
            waiter = ec2_client.get_waiter('instance_terminated')
            waiter.wait(InstanceIds=instance_ids)
            logger.info("  ✓ Instances terminated")
        
        logger.info("✓ EC2 instances deleted")
    except Exception as e:
        logger.error(f"Error deleting EC2 instances: {e}")

def delete_single_vpc(vpc_id: str):
    """Delete a single VPC and all its related resources."""
    logger.info(f"  Deleting VPC: {vpc_id}")
    
    try:
        # Delete VPC endpoints first - force deletion using AWS CLI if boto3 fails
        try:
            endpoints = ec2_client.describe_vpc_endpoints(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )
            for endpoint in endpoints["VpcEndpoints"]:
                if endpoint["State"] not in ["deleted", "deleting"]:
                    endpoint_id = endpoint["VpcEndpointId"]
                    try:
                        # Try using AWS CLI as fallback
                        import subprocess
                        result = subprocess.run([
                            "aws", "ec2", "delete-vpc-endpoints", 
                            "--vpc-endpoint-ids", endpoint_id,
                            "--region", region
                        ], capture_output=True, text=True)
                        
                        if result.returncode == 0:
                            logger.info(f"    ✓ Deleted VPC endpoint: {endpoint_id}")
                        else:
                            logger.warning(f"    Could not delete VPC endpoint {endpoint_id}: {result.stderr}")
                    except Exception as endpoint_error:
                        logger.warning(f"    Could not delete VPC endpoint {endpoint_id}: {endpoint_error}")
            
            # Wait longer for VPC endpoints to be deleted
            if endpoints["VpcEndpoints"]:
                logger.info("    Waiting for VPC endpoints to be deleted...")
                time.sleep(60)
        except Exception as e:
            logger.info(f"    Skipping VPC endpoint cleanup: {e}")
        
        # Delete network interfaces
        try:
            enis = ec2_client.describe_network_interfaces(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )
            for eni in enis["NetworkInterfaces"]:
                if eni["Status"] == "available":
                    ec2_client.delete_network_interface(NetworkInterfaceId=eni["NetworkInterfaceId"])
                    logger.info(f"    ✓ Deleted network interface: {eni['NetworkInterfaceId']}")
        except Exception as e:
            logger.warning(f"    Could not delete network interfaces: {e}")
        
        # Delete NAT gateways
        nat_gws = ec2_client.describe_nat_gateways(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )
        for nat_gw in nat_gws["NatGateways"]:
            if nat_gw["State"] != "deleted":
                ec2_client.delete_nat_gateway(NatGatewayId=nat_gw["NatGatewayId"])
                logger.info(f"    ✓ Deleted NAT Gateway: {nat_gw['NatGatewayId']}")
        
        # Wait for NAT gateways to be deleted
        time.sleep(30)
        
        # Release Elastic IPs
        eips = ec2_client.describe_addresses()
        for eip in eips["Addresses"]:
            if "NetworkInterfaceId" not in eip and "InstanceId" not in eip:
                try:
                    ec2_client.release_address(AllocationId=eip["AllocationId"])
                    logger.info(f"    ✓ Released EIP: {eip['AllocationId']}")
                except:
                    pass
        
        # Delete security groups
        sgs = ec2_client.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )
        for sg in sgs["SecurityGroups"]:
            if sg["GroupName"] != "default":
                try:
                    ec2_client.delete_security_group(GroupId=sg["GroupId"])
                    logger.info(f"    ✓ Deleted security group: {sg['GroupId']}")
                except:
                    pass
        
        # Delete subnets with retry
        subnets = ec2_client.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )
        for subnet in subnets["Subnets"]:
            subnet_id = subnet["SubnetId"]
            for attempt in range(3):
                try:
                    ec2_client.delete_subnet(SubnetId=subnet_id)
                    logger.info(f"    ✓ Deleted subnet: {subnet_id}")
                    break
                except ClientError as e:
                    if e.response["Error"]["Code"] == "DependencyViolation":
                        if attempt < 2:
                            logger.info(f"    Retrying subnet deletion in 30s: {subnet_id}")
                            time.sleep(30)
                        else:
                            logger.warning(f"    Could not delete subnet {subnet_id}: {e}")
                    else:
                        logger.warning(f"    Could not delete subnet {subnet_id}: {e}")
                        break
        
        # Delete route tables
        route_tables = ec2_client.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )
        for rt in route_tables["RouteTables"]:
            if not any(assoc.get("Main") for assoc in rt["Associations"]):
                ec2_client.delete_route_table(RouteTableId=rt["RouteTableId"])
                logger.info(f"    ✓ Deleted route table: {rt['RouteTableId']}")
        
        # Delete internet gateway
        igws = ec2_client.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        )
        for igw in igws["InternetGateways"]:
            ec2_client.detach_internet_gateway(
                InternetGatewayId=igw["InternetGatewayId"],
                VpcId=vpc_id
            )
            ec2_client.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
            logger.info(f"    ✓ Deleted internet gateway: {igw['InternetGatewayId']}")
        
        # Delete VPC with retry and complete cleanup
        vpc_deleted = False
        for attempt in range(3):
            try:
                ec2_client.delete_vpc(VpcId=vpc_id)
                logger.info(f"  ✓ VPC deletion initiated: {vpc_id}")
                
                # Wait and verify VPC deletion
                logger.info(f"    Waiting for VPC {vpc_id} to be deleted...")
                max_wait = 120  # Wait up to 2 minutes
                waited = 0
                while waited < max_wait:
                    try:
                        vpcs = ec2_client.describe_vpcs(VpcIds=[vpc_id])
                        if not vpcs.get("Vpcs"):
                            vpc_deleted = True
                            logger.info(f"  ✓ VPC {vpc_id} successfully deleted")
                            break
                        time.sleep(5)
                        waited += 5
                    except ClientError as check_error:
                        if check_error.response["Error"]["Code"] == "InvalidVpcID.NotFound":
                            vpc_deleted = True
                            logger.info(f"  ✓ VPC {vpc_id} successfully deleted")
                            break
                        raise
                
                if vpc_deleted:
                    break
                else:
                    logger.warning(f"    VPC {vpc_id} deletion timed out after {max_wait} seconds")
                    
            except ClientError as e:
                if e.response["Error"]["Code"] == "DependencyViolation":
                    if attempt < 2:
                        logger.info(f"    VPC has dependencies, cleaning up remaining resources (attempt {attempt + 1}/3)...")
                        
                        # Additional cleanup for remaining dependencies
                        try:
                            # Delete any remaining network ACLs
                            nacls = ec2_client.describe_network_acls(
                                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                            )
                            for nacl in nacls["NetworkAcls"]:
                                if not nacl["IsDefault"]:
                                    try:
                                        ec2_client.delete_network_acl(NetworkAclId=nacl["NetworkAclId"])
                                        logger.info(f"    ✓ Deleted network ACL: {nacl['NetworkAclId']}")
                                    except:
                                        pass
                            
                            # Delete any remaining DHCP options
                            dhcp_options = ec2_client.describe_dhcp_options()
                            for dhcp in dhcp_options["DhcpOptions"]:
                                try:
                                    ec2_client.disassociate_dhcp_options(VpcId=vpc_id)
                                    break
                                except:
                                    pass
                        except:
                            pass
                        
                        time.sleep(30)
                    else:
                        logger.error(f"  ✗ Failed to delete VPC {vpc_id} after 3 attempts: {e}")
                        break
                elif e.response["Error"]["Code"] == "InvalidVpcID.NotFound":
                    # VPC already deleted
                    vpc_deleted = True
                    logger.info(f"  ✓ VPC {vpc_id} already deleted")
                    break
                else:
                    logger.error(f"  ✗ Failed to delete VPC {vpc_id}: {e}")
                    break
        
        if not vpc_deleted:
            logger.error(f"  ✗ VPC {vpc_id} was not deleted. Please check dependencies manually.")
            # Final verification attempt
            try:
                vpcs = ec2_client.describe_vpcs(VpcIds=[vpc_id])
                if vpcs.get("Vpcs"):
                    logger.error(f"  ✗ VPC {vpc_id} still exists. Remaining resources may need manual cleanup.")
            except ClientError as final_check:
                if final_check.response["Error"]["Code"] == "InvalidVpcID.NotFound":
                    logger.info(f"  ✓ VPC {vpc_id} was actually deleted (final check)")
                else:
                    logger.error(f"  ✗ Could not verify VPC deletion status: {final_check}")
    except Exception as e:
        logger.error(f"Error deleting VPC {vpc_id}: {e}")

def delete_vpc_resources():
    """Delete VPC and related resources."""
    logger.info("[4/9] Deleting VPC resources")
    
    try:
        # Find all VPCs that might be related to the project
        vpc_name = f"vpc-for-{project_name}"
        
        # First, try to find VPCs by tag name
        vpcs_by_tag = ec2_client.describe_vpcs(
            Filters=[{"Name": "tag:Name", "Values": [vpc_name]}]
        )
        
        # Also get all VPCs to check for any that might be related
        all_vpcs = ec2_client.describe_vpcs()
        
        # Collect VPCs to delete
        vpcs_to_delete = []
        vpc_ids_found = set()
        
        # Add VPCs found by tag
        for vpc in vpcs_by_tag.get("Vpcs", []):
            vpc_id = vpc["VpcId"]
            if vpc_id not in vpc_ids_found:
                vpcs_to_delete.append(vpc_id)
                vpc_ids_found.add(vpc_id)
        
        # Check all VPCs for project-related resources (subnets, security groups, etc.)
        for vpc in all_vpcs.get("Vpcs", []):
            vpc_id = vpc["VpcId"]
            if vpc_id in vpc_ids_found:
                continue
            
            # First, check if VPC has the correct name tag
            vpc_has_name_tag = False
            for tag in vpc.get("Tags", []):
                if tag.get("Key") == "Name" and tag.get("Value") == vpc_name:
                    vpc_has_name_tag = True
                    vpcs_to_delete.append(vpc_id)
                    vpc_ids_found.add(vpc_id)
                    logger.info(f"  Found VPC by name tag: {vpc_id}")
                    break
            
            # If VPC has the correct name tag, skip checking resources
            if vpc_has_name_tag:
                continue
            
            # Check if VPC has project-related resources
            try:
                # Check subnets
                subnets = ec2_client.describe_subnets(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                )
                has_project_subnets = False
                for subnet in subnets.get("Subnets", []):
                    for tag in subnet.get("Tags", []):
                        if project_name in tag.get("Value", ""):
                            has_project_subnets = True
                            break
                    if has_project_subnets:
                        break
                
                # Check security groups
                sgs = ec2_client.describe_security_groups(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                )
                has_project_sgs = False
                for sg in sgs.get("SecurityGroups", []):
                    if project_name in sg.get("GroupName", ""):
                        has_project_sgs = True
                        break
                
                # Check NAT gateways
                nat_gws = ec2_client.describe_nat_gateways(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                )
                has_project_nat = False
                for nat_gw in nat_gws.get("NatGateways", []):
                    if nat_gw["State"] not in ["deleted", "deleting"]:
                        # Check tags
                        tags_response = ec2_client.describe_tags(
                            Filters=[
                                {"Name": "resource-id", "Values": [nat_gw["NatGatewayId"]]},
                                {"Name": "resource-type", "Values": ["nat-gateway"]}
                            ]
                        )
                        for tag in tags_response.get("Tags", []):
                            if project_name in tag.get("Value", ""):
                                has_project_nat = True
                                break
                        if has_project_nat:
                            break
                
                # If VPC has project-related resources, add it to deletion list
                if has_project_subnets or has_project_sgs or has_project_nat:
                    vpcs_to_delete.append(vpc_id)
                    vpc_ids_found.add(vpc_id)
                    logger.info(f"  Found project-related VPC: {vpc_id}")
            except Exception as e:
                logger.debug(f"  Error checking VPC {vpc_id}: {e}")
        
        if not vpcs_to_delete:
            logger.info("  No VPC found to delete")
            return
        
        logger.info(f"  Found {len(vpcs_to_delete)} VPC(s) to delete: {vpcs_to_delete}")
        
        # Delete each VPC
        for vpc_id in vpcs_to_delete:
            delete_single_vpc(vpc_id)
        
        # Final verification: Check if any VPCs still exist
        logger.info("  Verifying VPC deletion...")
        remaining_vpcs = []
        for vpc_id in vpcs_to_delete:
            try:
                vpcs = ec2_client.describe_vpcs(VpcIds=[vpc_id])
                if vpcs.get("Vpcs"):
                    remaining_vpcs.append(vpc_id)
                    logger.warning(f"  ⚠ VPC {vpc_id} still exists")

                    # retry VPC deletion
                    for attempt in range(3):
                        try:
                            ec2_client.delete_vpc(VpcId=vpc_id)
                            logger.info(f"  ✓ VPC deletion initiated: {vpc_id}")
                            break
                        except ClientError as e:
                            if e.response["Error"]["Code"] == "DependencyViolation":
                                if attempt < 2:
                                    logger.info(f"    Retrying VPC deletion in 30s: {vpc_id}")
                                    time.sleep(30)
                                else:
                                    logger.warning(f"    Could not delete VPC {vpc_id}: {e}")
                                    break
                            else:
                                logger.warning(f"    Could not delete VPC {vpc_id}: {e}")
                                break
            except ClientError as e:
                if e.response["Error"]["Code"] == "InvalidVpcID.NotFound":
                    logger.debug(f"  ✓ VPC {vpc_id} confirmed deleted")
                else:
                    logger.warning(f"  Could not verify VPC {vpc_id}: {e}")
        
        if remaining_vpcs:
            logger.error(f"  ✗ {len(remaining_vpcs)} VPC(s) still exist: {remaining_vpcs}")
            logger.error("  Please check AWS console and delete manually if needed")
        else:
            logger.info("✓ All VPC resources deleted")
    except Exception as e:
        logger.error(f"Error deleting VPC resources: {e}")

def delete_opensearch_collection():
    """Delete OpenSearch Serverless collection and policies."""
    logger.info("[5/9] Deleting OpenSearch collection")
    
    try:
        collection_name = project_name
        
        # Get collection ID first
        try:
            collections = opensearch_client.list_collections()
            collection_id = None
            for collection in collections.get("collectionSummaries", []):
                if collection["name"] == collection_name:
                    collection_id = collection["id"]
                    break
            
            if collection_id:
                # Delete collection using ID
                opensearch_client.delete_collection(id=collection_id)
                logger.info(f"  ✓ Deleted collection: {collection_name} (ID: {collection_id})")
                
                # Wait for deletion
                time.sleep(30)
            else:
                logger.info(f"  Collection {collection_name} not found")
                
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                logger.warning(f"  Could not delete collection: {e}")
        
        # Delete data access policy (different API)
        try:
            opensearch_client.delete_access_policy(
                name=f"data-{project_name}",
                type="data"
            )
            logger.info(f"  ✓ Deleted data access policy: data-{project_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                logger.warning(f"  Could not delete data access policy: {e}")
        
        # Delete policies
        policies = [
            ("network", f"net-{project_name}-{region}"),
            ("encryption", f"enc-{project_name}-{region}")
        ]
        
        for policy_type, policy_name in policies:
            try:
                opensearch_client.delete_security_policy(
                    name=policy_name,
                    type=policy_type
                )
                logger.info(f"  ✓ Deleted {policy_type} policy: {policy_name}")
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    logger.warning(f"  Could not delete {policy_type} policy {policy_name}: {e}")
        
        logger.info("✓ OpenSearch collection deleted")
    except Exception as e:
        logger.error(f"Error deleting OpenSearch collection: {e}")

def delete_knowledge_bases():
    """Delete Knowledge Bases and their data sources."""
    logger.info("[5.5/9] Deleting Knowledge Bases")
    
    try:
        # List all knowledge bases
        try:
            kb_list = bedrock_agent_client.list_knowledge_bases()
            knowledge_bases = kb_list.get("knowledgeBaseSummaries", [])
            
            # Find knowledge bases matching project name
            kb_to_delete = []
            for kb in knowledge_bases:
                if kb["name"] == project_name:
                    kb_to_delete.append(kb["knowledgeBaseId"])
                    logger.info(f"  Knowledge Base found: {kb['knowledgeBaseId']}")
                                
            if not kb_to_delete:
                logger.info(f"  No Knowledge Base found with name: {project_name}")
                return
            
            # Delete each knowledge base
            for kb_id in kb_to_delete:
                try:
                    logger.info(f"  Deleting Knowledge Base: {kb_id}")
                    
                    # Delete all data sources first
                    try:
                        data_sources = bedrock_agent_client.list_data_sources(
                            knowledgeBaseId=kb_id,
                            maxResults=100
                        )
                        for ds in data_sources.get("dataSourceSummaries", []):
                            try:
                                bedrock_agent_client.delete_data_source(
                                    knowledgeBaseId=kb_id,
                                    dataSourceId=ds["dataSourceId"]
                                )
                                logger.info(f"    ✓ Deleted data source: {ds['dataSourceId']}")
                            except Exception as e:
                                logger.warning(f"    Could not delete data source {ds['dataSourceId']}: {e}")
                    except Exception as e:
                        logger.debug(f"    Error listing/deleting data sources: {e}")
                    
                    # Delete the knowledge base
                    bedrock_agent_client.delete_knowledge_base(knowledgeBaseId=kb_id)
                    logger.info(f"  ✓ Deleted Knowledge Base: {kb_id}")
                    
                    # Wait for deletion to complete
                    logger.debug("    Waiting for Knowledge Base deletion to complete...")
                    max_wait = 60  # Wait up to 60 seconds
                    waited = 0
                    while waited < max_wait:
                        try:
                            kb_response = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=kb_id)
                            status = kb_response["knowledgeBase"]["status"]
                            if status == "DELETED":
                                break
                            time.sleep(5)
                            waited += 5
                        except ClientError as e:
                            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                                logger.debug("    Knowledge Base deletion confirmed")
                                break
                            raise
                    
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ResourceNotFoundException":
                        logger.debug(f"  Knowledge Base {kb_id} already deleted")
                    else:
                        logger.warning(f"  Could not delete Knowledge Base {kb_id}: {e}")
                except Exception as e:
                    logger.warning(f"  Error deleting Knowledge Base {kb_id}: {e}")
            
            logger.info("✓ Knowledge Bases deleted")
        except Exception as e:
            logger.warning(f"  Could not list Knowledge Bases: {e}")
            
    except Exception as e:
        logger.error(f"Error deleting Knowledge Bases: {e}")

def delete_s3_vectors_store():
    """Delete S3 Vectors index and vector bucket."""
    logger.info("[6/10] Deleting S3 Vectors store")

    try:
        try:
            s3vectors_client.delete_index(
                vectorBucketName=vector_bucket_name,
                indexName=vector_index_name,
            )
            logger.info(f"  ✓ Deleted vector index: {vector_index_name}")
            time.sleep(5)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NotFoundException":
                logger.warning(f"  Could not delete vector index {vector_index_name}: {e}")
            else:
                logger.info(f"  Vector index not found: {vector_index_name}")

        try:
            s3vectors_client.delete_vector_bucket(vectorBucketName=vector_bucket_name)
            logger.info(f"  ✓ Deleted vector bucket: {vector_bucket_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "NotFoundException":
                logger.warning(f"  Could not delete vector bucket {vector_bucket_name}: {e}")
            else:
                logger.info(f"  Vector bucket not found: {vector_bucket_name}")

        logger.info("✓ S3 Vectors store deleted")
    except Exception as e:
        logger.error(f"Error deleting S3 Vectors store: {e}")


def delete_secrets():
    """Delete Secrets Manager secrets."""
    logger.info("[7/10] Deleting secrets")
    
    secret_names = [
        f"openweathermap-{project_name}",
        f"langsmithapikey-{project_name}",
        f"tavilyapikey-{project_name}",
        f"perplexityapikey-{project_name}",
        f"firecrawlapikey-{project_name}",
        f"code-interpreter-{project_name}",
        f"novaactapikey-{project_name}",
        f"notionapikey-{project_name}"
    ]
    
    for secret_name in secret_names:
        try:
            secrets_client.delete_secret(
                SecretId=secret_name,
                ForceDeleteWithoutRecovery=True
            )
            logger.info(f"  ✓ Deleted secret: {secret_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                logger.warning(f"  Could not delete secret {secret_name}: {e}")
    
    logger.info("✓ Secrets deleted")

def delete_iam_roles():
    """Delete IAM roles and policies."""
    logger.info("[8/10] Deleting IAM roles")
    
    role_names = [
        f"role-knowledge-base-for-{project_name}-{region}",
        f"role-agent-for-{project_name}-{region}",
        f"role-ecs-task-for-{project_name}-{region}",
        f"role-ecs-execution-for-{project_name}-{region}",
        f"role-ec2-for-{project_name}-{region}",
        f"role-lambda-rag-for-{project_name}-{region}",
        f"role-agentcore-memory-for-{project_name}-{region}"
    ]
    
    for role_name in role_names:
        try:
            # Detach managed policies
            attached_policies = iam_client.list_attached_role_policies(RoleName=role_name)
            for policy in attached_policies["AttachedPolicies"]:
                iam_client.detach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy["PolicyArn"]
                )
            
            # Delete inline policies
            inline_policies = iam_client.list_role_policies(RoleName=role_name)
            for policy_name in inline_policies["PolicyNames"]:
                iam_client.delete_role_policy(
                    RoleName=role_name,
                    PolicyName=policy_name
                )
            
            # Remove from instance profile if exists
            instance_profile_name = f"instance-profile-{project_name}-{region}"
            try:
                iam_client.remove_role_from_instance_profile(
                    InstanceProfileName=instance_profile_name,
                    RoleName=role_name
                )
            except:
                pass
            
            # Delete role
            iam_client.delete_role(RoleName=role_name)
            logger.info(f"  ✓ Deleted role: {role_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                logger.warning(f"  Could not delete role {role_name}: {e}")
    
    # Delete instance profile
    try:
        instance_profile_name = f"instance-profile-{project_name}-{region}"
        iam_client.delete_instance_profile(InstanceProfileName=instance_profile_name)
        logger.info(f"  ✓ Deleted instance profile: {instance_profile_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            logger.warning(f"  Could not delete instance profile: {e}")
    
    logger.info("✓ IAM roles deleted")

def delete_s3_buckets():
    """Delete S3 buckets and all objects."""
    logger.info("[9/10] Deleting S3 buckets")
    
    # List of possible bucket names
    bucket_names = [
        bucket_name,  # storage-for-mcp-{account_id}-{region}
        f"storage-for-{project_name}--{region}"  # storage-for-mcp--us-west-2 (when account_id is empty)
    ]
    
    for bucket in bucket_names:
        try:
            # Delete all objects and versions
            try:
                # List and delete all object versions
                versions = s3_client.list_object_versions(Bucket=bucket)
                delete_keys = []
                
                # Add current versions
                if "Versions" in versions:
                    for version in versions["Versions"]:
                        delete_keys.append({
                            "Key": version["Key"],
                            "VersionId": version["VersionId"]
                        })
                
                # Add delete markers
                if "DeleteMarkers" in versions:
                    for marker in versions["DeleteMarkers"]:
                        delete_keys.append({
                            "Key": marker["Key"],
                            "VersionId": marker["VersionId"]
                        })
                
                # Delete in batches of 1000
                if delete_keys:
                    for i in range(0, len(delete_keys), 1000):
                        batch = delete_keys[i:i+1000]
                        s3_client.delete_objects(
                            Bucket=bucket,
                            Delete={"Objects": batch}
                        )
                    logger.info(f"  ✓ Deleted {len(delete_keys)} objects/versions from {bucket}")
                
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchBucket":
                    logger.warning(f"  Could not delete objects from {bucket}: {e}")
            
            # Delete bucket
            s3_client.delete_bucket(Bucket=bucket)
            logger.info(f"  ✓ Deleted bucket: {bucket}")
            
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchBucket":
                logger.info(f"  Bucket {bucket} does not exist")
            else:
                logger.warning(f"  Could not delete bucket {bucket}: {e}")
    
    logger.info("✓ S3 buckets deleted")

def main():
    """Main function to delete all infrastructure."""
    logger.info("="*60)
    logger.info("Starting AWS Infrastructure Cleanup")
    logger.info("="*60)
    logger.info(f"Project: {project_name}")
    logger.info(f"Region: {region}")
    logger.info(f"Account ID: {account_id}")
    logger.info("="*60)
    
    start_time = time.time()
    cleanup_errors = []
    
    try:
        if not delete_cloudfront_resources():
            cleanup_errors.append("CloudFront")
        delete_ecs_resources()
        delete_alb_resources()
        delete_ec2_instances()
        delete_vpc_resources()
        delete_opensearch_collection()
        delete_knowledge_bases()
        delete_s3_vectors_store()
        delete_secrets()
        delete_iam_roles()
        delete_s3_buckets()
        
        elapsed_time = time.time() - start_time
        logger.info("")
        logger.info("="*60)
        if cleanup_errors:
            logger.warning("Infrastructure Cleanup Completed With Warnings")
            logger.warning(f"Resources that may still remain: {', '.join(cleanup_errors)}")
            logger.warning("Re-run uninstaller.py after a few minutes if CloudFront is still deploying.")
        else:
            logger.info("Infrastructure Cleanup Completed Successfully!")
        logger.info("="*60)
        logger.info(f"Total cleanup time: {elapsed_time/60:.2f} minutes")
        logger.info("="*60)
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error("")
        logger.error("="*60)
        logger.error("Cleanup Failed!")
        logger.error("="*60)
        logger.error(f"Error: {e}")
        logger.error(f"Cleanup time before failure: {elapsed_time/60:.2f} minutes")
        logger.error("="*60)
        import traceback
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    main()
