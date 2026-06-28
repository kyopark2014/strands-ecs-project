# AWS Infrastructure Installer

boto3를 사용하여 AWS 인프라 리소스를 생성하는 Python 스크립트입니다.  
CDK 스택과 동등한 AWS 인프라를 프로그래밍 방식으로 배포합니다.

## 목차

1. [개요](#개요)
2. [설정값](#설정값)
3. [생성되는 리소스](#생성되는-리소스)
4. [주요 함수](#주요-함수)
5. [실행 방법](#실행-방법)
6. [배포 순서](#배포-순서)

---

## 개요

이 스크립트는 **Strands Agent + AgentCore Memory** 기반 Streamlit 채팅 애플리케이션을 위한 전체 AWS 인프라를 자동으로 생성합니다.

- **Streamlit UI** (`application/`) → ECS Fargate (Private Subnet)
- **Strands Agent** (`application/strands_agent.py`) → 동일 컨테이너에서 실행, **S3 Files**로 `/mnt/workspace` 세션 영속화

### 주요 특징

- **완전 자동화**: 단일 스크립트로 전체 인프라 배포
- **멱등성**: 이미 존재하는 리소스는 재사용
- **에러 핸들링**: 각 단계별 예외 처리 및 부분 배포 정보 저장
- **S3 Vectors 기반 RAG**: Bedrock Knowledge Base가 S3 Vectors를 벡터 스토어로 사용
- **ECS Fargate 배포**: Dockerfile 기반 이미지를 ECR에 push한 뒤 Fargate 서비스로 실행
- **AgentCore Memory**: short/long term memory 인스턴스 자동 생성
- **AgentCore Web Search Gateway**: MCP `websearch`용 Gateway (`us-east-1`)
- **S3 Files 세션 스토리지**: ECS 태스크에 `/mnt/workspace` volume 마운트 (`FileSessionManager` 연동)

### 사전 요구사항

- **Docker CLI**: 로컬에서 컨테이너 이미지 빌드 및 ECR push
- **AWS CLI**: ECR 로그인 (`aws ecr get-login-password`)
- **boto3**, **bedrock-agentcore** (`MemoryClient`) 및 AWS 자격 증명
- **IAM 권한**: EC2, IAM, VPC, ECS, ECR, CloudFront, Bedrock Agent, S3 Vectors, AgentCore Control/Memory, **S3 Files** (`s3files`)
- Knowledge Base 생성 시 `iam:PassRole` (Knowledge Base 서비스 역할)

---

## 설정값

```python
# 기본 설정
project_name = "strands-ecs"   # 프로젝트 이름 (최소 3자)
region = "us-west-2"           # AWS 리전
git_name = "strands-ecs-project"

# AgentCore Web Search Gateway
AGENTCORE_GATEWAY_REGION = "us-east-1"
AGENTCORE_WEBSEARCH_GATEWAY_NAME = "gateway-websearch"
AGENTCORE_WEBSEARCH_TARGET_NAME = "websearch"

# 자동 생성되는 변수
account_id = sts_client.get_caller_identity()["Account"]
bucket_name = f"storage-for-{project_name}-{account_id}-{region}"
vector_bucket_name = f"{project_name}-{account_id}"
vector_index_name = project_name

# 벡터 인덱스 설정
embedding_dimensions = 1024
embedding_data_type = "float32"
distance_metric = "cosine"

# S3 Files (ECS session storage)
S3_FILES_SESSION_PREFIX = "agentcore-sessions/"
SESSION_STORAGE_MOUNT_PATH = "/mnt/workspace"
S3_FILES_VOLUME_NAME = "session-storage"

# 커스텀 헤더 (CloudFront-ALB 통신용)
custom_header_name = "X-Custom-Header"
custom_header_value = f"{project_name}_12dab15e4s31"
```

---

## 생성되는 리소스

### 1. S3 버킷

- **이름**: `storage-for-{project_name}-{account_id}-{region}`
- **설정**:
  - CORS 활성화 (GET, POST, PUT)
  - 퍼블릭 액세스 차단
  - 버전 관리 **Enabled** (S3 Files file system 생성 필수)
  - `docs/` 폴더 자동 생성

### 2. IAM 역할

| 역할 | 설명 |
|------|------|
| `role-knowledge-base-for-{project_name}-{region}` | Bedrock Knowledge Base (S3, S3 Vectors, Bedrock) |
| `role-agent-for-{project_name}-{region}` | Bedrock Agent |
| `role-ecs-task-for-{project_name}-{region}` | ECS 태스크 (Bedrock, S3, Secrets Manager, **S3 Files mount** 등) |
| `role-ecs-execution-for-{project_name}-{region}` | ECS 실행 (ECR pull, CloudWatch Logs) |
| `role-agentcore-memory-for-{project_name}-{region}` | AgentCore Memory |
| `role-agentcore-gateway-websearch-for-{project_name}` | AgentCore Web Search Gateway (`us-east-1`) |
| `role-s3files-sync-for-{project_name}` | S3 Files ↔ S3 bucket 동기화 |

> `create_lambda_role()`, `create_opensearch_collection()` 등 레거시 함수는 코드에 남아 있으나 `main()`에서 호출되지 않습니다.

### 3. AgentCore Memory

- IAM 역할 + Memory 인스턴스 생성 (`create_agentcore_memory()`)
- `memory_id`를 `application/config.json`에 저장
- 사용자별 short/long term memory는 `user_{user_id}.json` + MCP로 연동

### 4. AgentCore Web Search Gateway

- **리전**: `us-east-1` (`AGENTCORE_GATEWAY_REGION`)
- **게이트웨이**: `gateway-websearch`
- MCP `websearch` 도구용 Gateway URL이 `application/config.json`에 기록

### 5. S3 Vectors (벡터 스토어)

- **벡터 버킷**: `{project_name}-{account_id}`
- **인덱스**: `{project_name}` (1024차원, cosine, float32)
- Bedrock 필수 메타데이터 키 non-filterable 설정

### 6. Bedrock Knowledge Base

- **스토리지**: S3 Vectors
- **임베딩**: Titan Embed Text v2
- **데이터 소스**: S3 `docs/` 프리픽스
- 기존 KB가 다른 스토리지면 삭제 후 재생성

### 7. VPC 네트워킹

```
VPC (10.20.0.0/16)
├── Public Subnets (2개 AZ)
│   ├── Internet Gateway
│   └── NAT Gateway
├── Private Subnets (2개 AZ)
│   └── NAT → ECR pull, Bedrock API 등
├── Security Groups
│   ├── ALB SG (포트 80)
│   ├── ECS SG (포트 8501, 443)
│   └── s3files-mount-sg-for-{project_name} (NFS 2049)
└── VPC Endpoints
    └── Bedrock Runtime
```

### 7.5. S3 Files (ECS Session Storage)

VPC 생성 직후 `create_s3_files_session_storage()`가 **멱등**으로 프로비저닝합니다.

| 리소스 | 설명 |
|--------|------|
| Sync IAM role | `role-s3files-sync-for-{project_name}` |
| File system | bucket `storage-for-...`, prefix `agentcore-sessions/` |
| Mount targets | private subnet마다 1개 |
| Access point | `/mnt/workspace` 마운트용 (posix uid/gid 0/0) |
| File system policy | ECS task role에 NFS mount/write 허용 |
| ECS task IAM | `s3files:ClientMount`, `GetAccessPoint`, `ListMountTargets` |
| ECS volume | `s3filesVolumeConfiguration` + `mountPoints` → `/mnt/workspace` |

`apply_s3_files_config()`가 `application/config.json`에 S3 Files 키를 기록합니다.

### 8. Application Load Balancer

- Internet-facing ALB, HTTP 80
- IP 타겟 그룹 (Fargate 8501)
- 헬스체크: `/_stcore/health`

### 9. CloudFront 배포

- **오리진**: ALB (기본), S3 (`/images/*`, `/docs/*`, `/artifacts/*`)
- Managed-CachingDisabled, HTTPS 리다이렉트

### 10. ECR

- **리포지토리**: `ecr-for-{project_name}`
- **플랫폼**: `linux/amd64`
- 타임스탬프 태그 + `latest`

### 11. ECS Fargate

- **클러스터**: `cluster-for-{project_name}`
- **서비스**: `service-for-{project_name}`
- **태스크**: `task-for-{project_name}`, CPU/Memory 1024/2048
- **컨테이너**: `app` (8501), **S3 Files volume** at `/mnt/workspace`
- **환경변수**: `APP_CONFIG_JSON` (전체 `config.json` 내용)
- **로그**: `/ecs/app-for-{project_name}`

---

## 주요 함수

### 인프라 생성

| 함수 | 설명 |
|------|------|
| `create_s3_bucket()` | S3 버킷, CORS, versioning **Enabled** |
| `create_knowledge_base_role()` / `create_agent_role()` / `create_ecs_roles()` | IAM 역할 |
| `create_agentcore_memory_role()` / `create_agentcore_memory()` | AgentCore Memory |
| `create_agentcore_websearch_gateway_role()` / `get_or_create_agentcore_websearch_gateway()` | Web Search Gateway |
| `create_s3_vectors_store()` | S3 Vectors 버킷·인덱스 |
| `create_knowledge_base_with_s3_vectors()` | Bedrock KB + S3 데이터 소스 |
| `create_vpc()` / `create_alb()` / `create_cloudfront_distribution()` | 네트워킹·CDN |
| `create_s3_files_session_storage()` | S3 Files (sync role, FS, mount targets, AP) |
| `attach_ecs_task_s3files_policy()` | ECS task role S3 Files inline policy |
| `apply_s3_files_config()` | config.json에 S3 Files 키 병합 |
| `create_ecr_repository()` / `build_and_push_docker_image()` | ECR 빌드·push |
| `deploy_ecs_service(..., s3_files_info=...)` | Fargate + S3 Files volume |
| `build_app_environment()` / `write_application_config()` | `application/config.json` |

### S3 Files 헬퍼

| 함수 | 설명 |
|------|------|
| `_get_or_create_s3files_sync_role()` | Sync IAM role |
| `_get_or_create_s3files_file_system()` | File system (`agentcore-sessions/`) |
| `_ensure_s3files_mount_targets()` | Mount targets per private subnet |
| `_get_or_create_s3files_access_point()` | Access point |
| `_ensure_s3files_file_system_policy()` | ECS task role NFS policy |
| `_ensure_s3_bucket_versioning_enabled()` | Bucket versioning Enabled |

### 기타 헬퍼

| 함수 | 설명 |
|------|------|
| `s3_vectors_bucket_arn()` / `s3_vectors_index_arn()` | S3 Vectors ARN |
| `create_ecs_log_group()` / `create_ecs_cluster()` | ECS 로그·클러스터 |
| `create_alb_target_group_for_ecs()` | IP 타겟 그룹 |
| `check_application_ready()` | CloudFront 헬스 확인 |
| `build_config_from_deployment_state()` | 부분 배포 config 생성 |

### 레거시 (`main()` 미사용)

`create_opensearch_collection()`, `create_lambda_role()`, `create_ec2_instance()`, `--run-setup`, `--verify-deployment` 등

---

## 실행 방법

### 기본 실행

```bash
pip install boto3 bedrock-agentcore
python installer.py
```

로컬 Docker로 Streamlit 이미지를 빌드하고 ECR push → ECS Fargate 배포.

### Docker 빌드 생략

```bash
python installer.py --skip-docker-build
```

ECR `{repository_uri}:latest` 재사용.

### 레거시 EC2

```bash
python installer.py --run-setup
python installer.py --verify-deployment
```

---

## 배포 순서

```
[1/10] S3 버킷 생성 (versioning Enabled)
       ↓
[2/10] IAM 역할 + AgentCore 리소스
       • Knowledge Base / Agent / ECS Task·Execution 역할
       • AgentCore Memory 역할 + Memory 인스턴스
       • AgentCore Web Search Gateway 역할 + Gateway
       ↓
[3/10] S3 Vectors 스토어
       ↓
[4.5/10] Bedrock Knowledge Base + S3 데이터 소스 (docs/)
       ↓
[5/10] VPC (서브넷, NAT, ALB/ECS SG, Bedrock endpoint)
       ↓
[5.5/10] S3 Files 세션 스토리지
       • sync role, file system, mount targets, access point
       • ECS task role S3 Files policy
       • application/config.json S3 Files 키
       ↓
[6/10] Application Load Balancer
       ↓
[7/10] CloudFront (ALB + S3 하이브리드)
       ↓
[8/10] application/config.json 생성 + ECR Docker build/push
       ↓
[9/10] ECS Fargate 서비스 (S3 Files volume @ /mnt/workspace)
       ↓
[10/10] CloudFront URL 준비 상태 확인
       ↓
완료 - application/config.json 갱신
```

---

## 배포 완료 후

```
================================================================
Infrastructure Deployment Completed Successfully!
================================================================
Summary:
  S3 Bucket: storage-for-strands-ecs-{account_id}-us-west-2
  VPC ID: vpc-xxxxxxxxx
  ALB DNS: http://alb-for-strands-ecs-....elb.amazonaws.com/
  CloudFront Domain: https://xxxxxx.cloudfront.net
  ECS Service: service-for-strands-ecs (Fargate, /mnt/workspace mounted)
  ECR Image: .../ecr-for-strands-ecs:...
  S3 Vector Bucket / Index ARN: ...
  Knowledge Base ID: ...
  AgentCore Memory ID: ...
  AgentCore Web Search Gateway: gateway-websearch (...)
  S3 Files Access Point: arn:aws:s3files:...
  ECS Session Mount Path: /mnt/workspace
================================================================
```

### application/config.json

| 필드 | 설명 |
|------|------|
| `projectName`, `accountId`, `region` | 기본 정보 |
| `knowledge_base_id`, `data_source_id`, `knowledge_base_role` | Bedrock KB |
| `vector_bucket_*`, `vector_index_*` | S3 Vectors |
| `s3_bucket`, `s3_arn` | 문서 S3 버킷 |
| `agentcore_memory_role`, `memory_id` | AgentCore Memory |
| `agentcore_websearch_gateway_*` | Web Search Gateway |
| `s3_files_file_system_id`, `s3_files_access_point_arn` | S3 Files |
| `s3_files_mount_path` | `/mnt/workspace` |
| `ecs_session_vpc_subnets`, `ecs_session_security_groups` | ECS 세션 네트워크 |
| `sharing_url` | CloudFront URL |

ECS 컨테이너: `APP_CONFIG_JSON` → `docker-entrypoint.sh` → `application/config.json`

### Docker Container

Streamlit UI + Strands Agent. 프로젝트 루트 `Dockerfile` (`linux/amd64`, Python 3.13, Node.js for npx/MCP).

헬스체크: `curl -f http://localhost:8501/_stcore/health`

### 주의사항

- CloudFront·ECS 안정화에 15–20분 소요될 수 있음
- Private Subnet Fargate는 NAT Gateway로 ECR pull
- S3 bucket **versioning Enabled** 필수 (S3 Files)
- ECS task와 S3 Files mount target은 **동일 VPC private subnet**, SG **2049** 필요
- KB가 OpenSearch Serverless 사용 중이면 S3 Vectors로 자동 마이그레이션(삭제 후 재생성)
- 레거시 `instance` 타입 ALB 타겟 그룹이 있으면 ECS 배포 전 삭제 필요

---

## 에러 처리

| 상황 | 처리 |
|------|------|
| 리소스 이미 존재 | 재사용 |
| KB 스토리지 불일치 | KB 삭제 후 S3 Vectors 재생성 |
| ECS 서비스 존재 | 새 태스크 정의로 `forceNewDeployment` |
| S3 Files FS 생성 실패 | bucket versioning 미활성 → `_ensure_s3_bucket_versioning_enabled()` |
| 배포 실패 | 부분 정보를 `application/config.json`에 저장 |

---

## 인프라 삭제

```bash
python uninstaller.py
```

삭제 순서(요약): CloudFront → ECS → ALB → EC2(레거시) → **S3 Files** → VPC → KB / S3 Vectors → Gateway / Memory / IAM → S3 bucket → `application/config.json`

옵션: `--yes`, `--delete-agentcore-gateway`
