# Infra Scripts Guide

## Overview
- Create necessary infrastructure files using **AWS CloudFormation** and **SAM templates** as applicable.
- Ensure the deployment is re-deployable with a corresponding destroy script for cleanup.

## Configuration Details
- **AWS Region:** `us-east-1`
- **Environment:** `dev`
- **Resource Naming Convention:** `<environment>-lmarbiter-<resourcetype>`
  - Examples:
    - `dev-lmarbiter-vpc`
    - `dev-lmarbiter-subnet`
    - `dev-lmarbiter-lambda`

## Step 1: Create a Non-Person IAM User
- Avoid granting full administrative access.
- Restrict scope to necessary deployment permissions.
- Limit IAM management:
  - Do not grant `iam:*` permissions globally.
  - Use IAM Permission Boundaries to allow creation of application roles (e.g., Lambda execution roles) without elevating permissions or creating admin accounts.
- Use IAM Policy Conditions (`aws:RequestedRegion`, `aws:SourceVpc`) to restrict execution from specific regions or VPCs.
- Enforce CloudFormation Service Roles:
  - Allow passing a specific CloudFormation Service Role (`cloudformation:ExecuteChangeSet` with `iam:PassRole`).
  - Do not give direct permissions to create EC2, VPC, or RDS instances.
  - Tailor templates for headless execution due to non-interactive nature of the user.

## Step 2: Provide Safe Defaults and Parameter Management
- Set realistic default values for parameters to ensure scripts run smoothly even if optional flags are omitted.
- Build parameter files (e.g., `dev.json`) for environment configurations.
- Use Change Sets for safety:
  - Run `aws cloudformation create-change-set` first,
  - Log upcoming changes,
  - Then execute the change set.

## Step 3: Implement Modular Architecture
- Avoid monolithic templates; break down infrastructure into smaller, manageable modules.
- Decouple base network from application components:
  - Example base network template: `vpc-template.yaml`
  - Application-specific templates (e.g., ECS service): `ecs-service.yaml`
- Leverage Parameters & Mappings:
  - Do not hardcode environment-specific values like VPC IDs, CIDR blocks, or instance sizes.
  - Use Parameters section for user inputs and Mappings for environment configurations.
p-	Utilize AWS-specific parameter types such as:
p-	`AWS::EC2::VPC::Id`,
p-	`AWS::EC2::KeyPair::KeyName`. 
p-	CloudFormation validates these inputs against your AWS account before execution, ensuring early failure if resources do not exist.
