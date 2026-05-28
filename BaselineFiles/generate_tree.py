import os
import json

# Define the file contents mapping
file_data = {
    # === ZSCALER ===
    "zscaler/LM_ZIA_URL_Categories.json": {
        "urlCategories": [
            {
                "id": "LM-ZIA-URLCAT-CLOUD-BLK-004",
                "name": "Blocked Cloud Storage",
                "description": "Cloud storage services blocked per security policy",
                "type": "URL_CATEGORY",
                "urls": ["dropbox.com", "*.dropbox.com", "dropboxusercontent.com"],
                "dbCategorizedUrls": ["dropbox.com"],
                "action": "BLOCK",
                "customIpRangesCount": 0,
                "urlsRetainingParentCategoryCount": 0,
                "lastModified": "2026-01-15T10:23:00Z",
            }
        ]
    },
    "zscaler/LM_ZIA_DLP_Policies.json": {
        "dlpPolicies": [
            {
                "id": "LM-ZIA-DLP-001",
                "name": "SSN and DOB Block",
                "description": "Block SSN and date-of-birth transmission to external domains",
                "patterns": ["\\b\\d{3}-\\d{2}-\\d{4}\\b", "\\b\\d{2}/\\d{2}/\\d{4}\\b"],
                "action": "BLOCK",
                "exceptedDomains": [],
            }
        ]
    },
    "zscaler/LM_ZIA_Firewall_Rules.json": [],  # Kept as valid empty array due to cut-off text

    # === SHAREPOINT (TXT POLICIES) ===
    "sharepoint/network-standards/LM-STD-NET-SEG-005_v2.1.txt": (
        "Liberty Mutual Network Segmentation Standard\n"
        "Document ID: LM-STD-NET-SEG-005 Version: 2.1\n"
        "Approved by: NSEC Committee — 2025-11-01\n"
        "Section 2.3 Production Environment Isolation\n"
        "Production segments must not be accessible from development environments under any "
        "circumstances. No exceptions without NSEC approval.\n"
        "Production VPC CIDR: 172.16.0.0/16\n"
        "Development VPC CIDR: 10.50.0.0/16\n"
        "All VPC peering connections between production (172.16.0.0/16) and development "
        "(10.50.0.0/16) are strictly prohibited."
    ),
    "sharepoint/access-policies/LM-POL-ACCESS-ENT-001_v2.1.txt": (
        "Liberty Mutual Enterprise Access Management Policy\n"
        "Document ID: LM-POL-ACCESS-ENT-001 Version: 2.1 STATUS: CURRENT\n"
        "Approved by: CISO — 2025-09-15\n"
        "Section 4.1 Access Review Requirements\n"
        "All privileged access accounts must undergo formal access review every 90 days. "
        "Access reviews are mandatory and non-compliance constitutes a policy violation.\n"
        "Review cycle: 90 days (quarterly)"
    ),
    "sharepoint/access-policies/LM-POL-ACCESS-ENT-001_v1.8.txt": (
        "Liberty Mutual Enterprise Access Management Policy\n"
        "Document ID: LM-POL-ACCESS-ENT-001 Version: 1.8 STATUS: SUPERSEDED\n"
        "Approved by: CISO — 2024-06-01\n"
        "Section 4.1 Access Review Requirements\n"
        "All privileged access accounts must undergo formal access review every 60 days. "
        "Access reviews are mandatory and non-compliance constitutes a policy violation.\n"
        "Review cycle: 60 days (bi-monthly)"
    ),
    "sharepoint/compliance-and-regulatory/LM-POL-DATA-RESID-007_v1.1.txt": (
        "Liberty Mutual Data Residency Policy\n"
        "Document ID: LM-POL-DATA-RESID-007 Version: 1.1\n"
        "Regulatory Framework: NAIC MDL-668, state insurance regulations\n"
        "Section 2.1 Data Storage Requirements\n"
        "All customer data including claims records, policyholder PII, and actuarial data "
        "must be stored exclusively in US-based AWS regions. "
        "Cross-region replication to non-US regions is strictly prohibited. "
        "Permitted regions: us-east-1, us-east-2, us-west-1, us-west-2. "
        "Violation of this policy may require notification to state insurance regulators."
    ),

    # === AWS CONFIG ===
    "aws-config/by-resource-type/SecurityGroups.json": {
        "resourceType": "AWS::EC2::SecurityGroup",
        "resources": [
            {
                "resourceId": "sg-lm-prod-peer-dev-001",
                "resourceName": "lm-prod-peer-dev",
                "configuration": {
                    "groupId": "sg-lm-prod-peer-dev-001",
                    "groupName": "lm-prod-peer-dev",
                    "vpcId": "vpc-lm-prod-001a2b3c4d",
                    "description": "Temporary peering rule — prod to dev",
                    "ipPermissions": [
                        {
                            "ipProtocol": "-1",
                            "fromPort": -1,
                            "toPort": -1,
                            "ipRanges": [{"cidrIp": "10.50.0.0/16", "description": "dev VPC CIDR"}],
                        }
                    ],
                    "createdAt": "2026-03-15T09:00:00Z",
                    "daysActive": 61,
                    "environment": "PRODUCTION",
                },
            }
        ],
    },
    "aws-config/by-resource-type/S3_Buckets.json": {
        "resourceType": "AWS::S3::Bucket",
        "resources": [
            {
                "resourceId": "lm-prod-claims-data-primary",
                "resourceName": "lm-prod-claims-data-primary",
                "configuration": {
                    "bucketName": "lm-prod-claims-data-primary",
                    "region": "us-east-1",
                    "dataClassification": "CONFIDENTIAL",
                    "containsCustomerData": True,
                    "dataTypes": ["claims", "PII", "actuarial"],
                    "replicationConfiguration": {
                        "rules": [
                            {
                                "id": "dr-eu-replication",
                                "status": "Enabled",
                                "destination": {
                                    "bucket": "arn:aws:s3:::lm-prod-claims-data-eu-dr",
                                    "region": "eu-west-1",
                                },
                                "prefix": "",
                            }
                        ]
                    },
                    "createdAt": "2025-12-04T00:00:00Z",
                    "replicationActiveFor": 134,
                    "legalNotified": False,
                },
            }
        ],
    },
    "aws-config/by-resource-type/VPCs.json": {
        "resourceType": "AWS::EC2::VPC",
        "resources": [
            {"resourceId": "vpc-lm-prod-001a2b3c4d", "cidrBlock": "172.16.0.0/16", "environment": "PRODUCTION"},
            {"resourceId": "vpc-lm-dev-002b3c4d5e", "cidrBlock": "10.50.0.0/16", "environment": "DEVELOPMENT"},
            {"resourceId": "vpc-lm-dmz-003c4d5e6f", "cidrBlock": "192.168.0.0/16", "environment": "DMZ"},
        ],
    },
    "aws-config/by-resource-type/IAM_Policies.json": {
        "resourceType": "AWS::IAM::Policy",
        "resources": [
            {
                "resourceId": "pol-claims-app-access",
                "policyName": "ClaimsAppAccess",
                "attachedTo": ["claims-app-role"],
                "permissions": ["s3:GetObject", "s3:PutObject", "dynamodb:*"],
                "resources": ["arn:aws:s3:::lm-prod-claims-data-primary/*"],
            }
        ],
    }
}

def create_structure():
    print("🚀 Starting folder and file tree generation...\n")
    
    for filepath, content in file_data.items():
        # Extract the directory component from the filepath
        dirname = os.path.dirname(filepath)
        
        # Create directory path if it doesn't exist
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname, exist_ok=True)
            print(created_msg := f"📁 Created directory: {dirname}")
            
        # Write content based on file extension
        try:
            if filepath.endswith('.json'):
                with open(filepath, 'w', encoding='utf-8') as json_file:
                    json.dump(content, json_file, indent=2)
            else:
                with open(filepath, 'w', encoding='utf-8') as text_file:
                    text_file.write(content)
            print(f"📄 Generated file: {filepath}")
        except Exception as e:
            print(f"❌ Failed to write {filepath}. Error: {e}")

    print("\n✅ Tree generation complete!")

if __name__ == "__main__":
    create_structure()
