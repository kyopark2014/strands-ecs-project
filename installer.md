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

이 스크립트는 AI 기반 채팅 애플리케이션을 위한 전체 AWS 인프라를 자동으로 생성합니다.

### 주요 특징
- **완전 자동화**: 단일 스크립트로 전체 인프라 배포
- **멱등성**: 이미 존재하는 리소스는 재사용
- **에러 핸들링**: 각 단계별 예외 처리 및 롤백 지원
- **로깅**: 상세한 배포 진행 상황 출력
- **S3 Vectors 기반 RAG**: Bedrock Knowledge Base가 OpenSearch Serverless 대신 S3 Vectors를 벡터 스토어로 사용
- **ECS Fargate 배포**: Dockerfile 기반 이미지를 ECR에 push한 뒤 ECS Fargate 서비스로 실행

### 사전 요구사항
- **Docker CLI**: 로컬에서 컨테이너 이미지 빌드 및 ECR push
- **AWS CLI**: ECR 로그인 (`aws ecr get-login-password`)
- **boto3** 및 스크립트 실행에 필요한 AWS 자격 증명

---

## 설정값

```python
# 기본 설정
project_name = "sam"           # 프로젝트 이름 (최소 3자)
region = "us-west-2"           # AWS 리전
git_name = "sam-project"       # Git 저장소 이름 (레거시 EC2 SSM 배포용)

# 자동 생성되는 변수
account_id = sts_client.get_caller_identity()["Account"]
bucket_name = f"storage-for-{project_name}-{account_id}-{region}"
vector_bucket_name = f"{project_name}-{account_id}"
vector_index_name = project_name

# 벡터 인덱스 설정
embedding_dimensions = 1024
embedding_data_type = "float32"
distance_metric = "cosine"

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
  - 버전 관리 Suspended
  - `docs/` 폴더 자동 생성

### 2. IAM 역할

| 역할 | 설명 |
|------|------|
| `role-knowledge-base-for-{project_name}-{region}` | Bedrock Knowledge Base용 역할 (S3 Vectors 접근 포함) |
| `role-agent-for-{project_name}-{region}` | Bedrock Agent용 역할 |
| `role-ecs-task-for-{project_name}-{region}` | ECS 태스크용 역할 (Bedrock, S3, Secrets Manager 등 앱 권한) |
| `role-ecs-execution-for-{project_name}-{region}` | ECS 태스크 실행 역할 (ECR pull, CloudWatch Logs) |
| `role-agentcore-memory-for-{project_name}-{region}` | AgentCore Memory용 역할 |

> `create_lambda_role()` 함수는 코드에 남아 있으나, 현재 `main()` 배포 흐름에서는 호출되지 않습니다.

### 3. Secrets Manager
- `tavilyapikey-{project_name}`: Tavily API 키 (실행 시 프롬프트로 입력, Enter로 건너뛰기 가능)

### 4. S3 Vectors (벡터 스토어)
- **벡터 버킷**: `{project_name}-{account_id}`
- **인덱스**: `{project_name}` (1024차원, cosine, float32)
- **메타데이터**: Bedrock 필수 키(`AMAZON_BEDROCK_TEXT`, `AMAZON_BEDROCK_METADATA`)를 non-filterable로 설정

### 5. VPC 네트워킹

```
VPC (10.20.0.0/16)
├── Public Subnets (2개 AZ)
│   ├── Internet Gateway 연결
│   └── NAT Gateway 호스팅
├── Private Subnets (2개 AZ)
│   └── NAT Gateway를 통한 아웃바운드 (ECR pull, Bedrock API 등)
├── Security Groups
│   ├── ALB SG (포트 80)
│   └── ECS SG (포트 8501, 443)
└── VPC Endpoints
    └── Bedrock Runtime 엔드포인트
```

### 6. Application Load Balancer
- **타입**: Internet-facing Application Load Balancer
- **리스너**: HTTP 포트 80
- **타겟 그룹**: ECS Fargate 태스크 (IP 타겟, 포트 8501)
- **헬스체크**: `/_stcore/health`

### 7. CloudFront 배포
- **오리진**:
  - 기본: ALB (동적 컨텐츠)
  - `/images/*`, `/docs/*`: S3 (정적 컨텐츠)
- **캐시 정책**: Managed-CachingDisabled
- **프로토콜**: HTTP → HTTPS 리다이렉트

### 8. ECR (Elastic Container Registry)
- **리포지토리**: `ecr-for-{project_name}`
- **이미지 태그**: `latest`
- **플랫폼**: `linux/amd64`
- **빌드 소스**: 프로젝트 루트의 `Dockerfile`

### 9. ECS Fargate
- **클러스터**: `cluster-for-{project_name}`
- **서비스**: `service-for-{project_name}`
- **태스크 정의**: `task-for-{project_name}`
- **컨테이너**: `app` (포트 8501)
- **CPU / Memory**: 1024 / 2048
- **배포 위치**: Private Subnet (퍼블릭 IP 없음)
- **로그**: CloudWatch Logs `/ecs/app-for-{project_name}`

### 10. Bedrock Knowledge Base
- **스토리지**: S3 Vectors (`S3_VECTORS` 타입)
- **임베딩 모델**: Amazon Titan Embed Text v2 (1024차원, FLOAT32)
- **파싱**: 기본 파서 (default parser)
- **청킹**: Fixed Size (300 토큰, 20% 오버랩)
- **데이터 소스**: S3 `docs/` 프리픽스

> `create_opensearch_collection()` 함수는 이전 버전 호환을 위해 코드에 남아 있으나, 현재 배포 흐름에서는 사용하지 않습니다.

---

## 주요 함수

### 인프라 생성 함수

#### `create_s3_bucket()`
S3 버킷 생성 및 CORS, 퍼블릭 액세스 차단 설정

```python
def create_s3_bucket() -> str:
    """Create S3 bucket with CORS configuration."""
    # 버킷 생성
    # CORS 설정 (GET, POST, PUT 허용)
    # 퍼블릭 액세스 차단
    # docs/ 폴더 생성
    return bucket_name
```

#### `create_iam_role()`
IAM 역할 생성 및 관리형 정책 연결

```python
def create_iam_role(role_name: str, assume_role_policy: Dict,
                    managed_policies: Optional[List[str]] = None) -> str:
    """Create IAM role."""
    # 역할 생성
    # Trust Policy 설정
    # 관리형 정책 연결
    return role_arn
```

#### `create_knowledge_base_role()` / `create_agent_role()` / `create_ecs_roles()` / `create_agentcore_memory_role()`
각 서비스별 IAM 역할 및 인라인 정책 생성

`create_ecs_roles()`는 아래 두 역할을 반환합니다.

```python
{
    "task_role_arn": "...",
    "execution_role_arn": "...",
}
```

#### `create_s3_vectors_store()`
S3 Vectors 벡터 버킷 및 인덱스 생성

```python
def create_s3_vectors_store() -> Dict[str, str]:
    """Create S3 vector bucket and index for Bedrock Knowledge Base."""
    # 벡터 버킷 생성
    # 벡터 인덱스 생성 (1024차원, cosine)
    return {
        "vectorBucketName": vector_bucket_name,
        "vectorBucketArn": vector_bucket_arn,
        "indexName": vector_index_name,
        "indexArn": index_arn,
    }
```

#### `create_knowledge_base_with_s3_vectors()`
S3 Vectors를 스토리지로 사용하는 Bedrock Knowledge Base 생성

```python
def create_knowledge_base_with_s3_vectors(
    s3_vectors_info: Dict[str, str],
    knowledge_base_role_arn: str,
    s3_bucket_name: str,
) -> Tuple[str, str]:
    """Create Knowledge Base with S3 Vectors as the vector store."""
    # 기존 KB가 다른 스토리지를 사용하면 삭제 후 재생성
    # Knowledge Base 생성 (Titan Embed v2)
    # S3 데이터 소스 생성 (docs/ 프리픽스)
    return knowledge_base_id, data_source_id
```

#### `create_vpc()`
VPC, 서브넷, 보안 그룹, VPC 엔드포인트 생성

```python
def create_vpc() -> Dict[str, str]:
    """Create VPC with subnets and security groups."""
    # VPC 생성 (DNS 활성화)
    # 퍼블릭/프라이빗 서브넷 생성
    # Internet Gateway, NAT Gateway 생성
    # 보안 그룹 생성
    # Bedrock Runtime VPC 엔드포인트 생성
    return {
        "vpc_id": vpc_id,
        "public_subnets": public_subnets,
        "private_subnets": private_subnets,
        "alb_sg_id": alb_sg_id,
        "ecs_sg_id": ecs_sg_id,
    }
```

#### `create_alb()`
Application Load Balancer 생성

```python
def create_alb(vpc_info: Dict[str, str]) -> Dict[str, str]:
    """Create Application Load Balancer."""
    # 최소 2개 AZ의 퍼블릭 서브넷 검증
    # 보안 그룹 연결
    # Internet-facing ALB 생성
    return {"arn": alb_arn, "dns": alb_dns}
```

#### `create_cloudfront_distribution()`
CloudFront 배포 생성 (ALB + S3 하이브리드)

```python
def create_cloudfront_distribution(alb_info: Dict[str, str],
                                   s3_bucket_name: str) -> Dict[str, str]:
    """Create CloudFront distribution with hybrid ALB + S3 origins."""
    # Origin Access Identity 생성
    # S3 버킷 정책 업데이트
    # CloudFront 배포 생성
    #   - 기본 오리진: ALB
    #   - /images/*, /docs/*: S3
    return {"id": distribution_id, "domain": distribution_domain}
```

#### `create_ecr_repository()`
ECR 리포지토리 생성

```python
def create_ecr_repository() -> str:
    """Create ECR repository and return repository URI."""
    # ecr-for-{project_name} 생성
    return repository_uri
```

#### `build_and_push_docker_image()`
로컬 Docker로 이미지 빌드 후 ECR push

```python
def build_and_push_docker_image(repository_uri: str, image_tag: str = "latest") -> str:
    """Build Docker image from Dockerfile and push to ECR."""
    # aws ecr get-login-password 로 Docker 로그인
    # docker build --platform linux/amd64
    # docker push
    return image_uri
```

#### `deploy_ecs_service()`
ECS Fargate 서비스 배포 (태스크 정의, ALB 연동 포함)

```python
def deploy_ecs_service(
    vpc_info: Dict[str, str],
    alb_info: Dict[str, str],
    ecs_roles: Dict[str, str],
    image_uri: str,
    app_environment: Dict[str, str],
    log_group_name: str,
) -> Dict[str, str]:
    """Create ECS task definition and Fargate service behind the ALB."""
    # ECS 클러스터 생성
    # IP 타겟 그룹 생성
    # ALB 리스너 및 커스텀 헤더 규칙 생성
    # 태스크 정의 등록 (APP_CONFIG_JSON 환경변수 포함)
    # Fargate 서비스 생성/업데이트
    return {
        "cluster_arn": cluster_arn,
        "service_arn": service_arn,
        "service_name": service_name,
        "task_definition_arn": task_definition_arn,
        "target_group_arn": tg_arn,
        "listener_arn": listener_arn,
    }
```

#### `build_app_environment()`
컨테이너 런타임에 주입할 `application/config.json` 내용 생성

### 헬퍼 함수

| 함수 | 설명 |
|------|------|
| `s3_vectors_bucket_arn()` / `s3_vectors_index_arn()` | S3 Vectors ARN 생성 |
| `attach_inline_policy()` | IAM 역할에 인라인 정책 연결 |
| `create_secrets()` | Secrets Manager 시크릿 생성 |
| `ensure_data_source()` | Knowledge Base S3 데이터 소스 생성/조회 |
| `delete_knowledge_base()` | Knowledge Base 및 데이터 소스 삭제 |
| `create_security_group()` | 보안 그룹 생성 |
| `create_vpc_endpoint()` | VPC 엔드포인트 생성 |
| `create_public_subnets()` / `create_private_subnets()` | 서브넷 생성 |
| `get_or_create_internet_gateway()` / `get_or_create_nat_gateway()` | IGW/NAT Gateway 조회/생성 |
| `classify_subnets()` | 서브넷을 퍼블릭/프라이빗으로 분류 |
| `wait_for_subnet_available()` / `wait_for_nat_gateway()` | 리소스 가용 상태 대기 |
| `create_ecs_log_group()` | ECS CloudWatch Logs 그룹 생성 |
| `create_ecs_cluster()` | ECS 클러스터 생성 |
| `create_alb_target_group_for_ecs()` | Fargate용 IP 타겟 그룹 생성 |
| `create_alb_listener_with_target_group()` | ALB 리스너 및 커스텀 헤더 규칙 생성 |
| `check_application_ready()` | CloudFront URL 애플리케이션 준비 상태 확인 |

### 레거시 EC2 함수 (main()에서 미사용)

| 함수 | 설명 |
|------|------|
| `get_setup_script()` | EC2 User Data / SSM 설정 스크립트 생성 |
| `run_setup_script_via_ssm()` | SSM Run Command로 설정 스크립트 실행 |
| `create_ec2_instance()` | EC2 인스턴스 생성 |
| `create_alb_target_group_and_listener()` | EC2 instance 타겟 그룹 등록 |
| `verify_ec2_subnet_deployment()` | EC2 서브넷 배포 검증 |

---

## 실행 방법

### 기본 실행 (전체 인프라 배포)

```bash
python installer.py
```

로컬 Docker로 이미지를 빌드하고 ECR에 push한 뒤 ECS Fargate 서비스를 생성합니다.

### Docker 빌드 생략 (기존 ECR 이미지 재사용)

```bash
python installer.py --skip-docker-build
```

ECR의 `{repository_uri}:latest` 이미지를 그대로 사용합니다. 인프라만 재배포하거나 태스크 정의만 갱신할 때 유용합니다.

### 레거시: 기존 EC2 인스턴스에 설정 스크립트 실행

```bash
# 인스턴스 이름으로 자동 탐색
python installer.py --run-setup

# 특정 인스턴스 ID 지정
python installer.py --run-setup i-1234567890abcdef0
```

> 현재 기본 배포는 ECS Fargate입니다. `--run-setup`은 이전 EC2 배포 환경 호환용입니다.

### 레거시: EC2 서브넷 배포 검증

```bash
python installer.py --verify-deployment
```

---

## 배포 순서

스크립트는 다음 순서로 리소스를 생성합니다:

```
[1/10] S3 버킷 생성
       ↓
[2/10] IAM 역할 생성
       • Knowledge Base 역할
       • Agent 역할
       • ECS Task / Execution 역할
       • AgentCore Memory 역할
       ↓
[3/10] Secrets Manager 시크릿 생성
       • Tavily API 키 (선택 입력)
       ↓
[4/10] S3 Vectors 스토어 생성
       • 벡터 버킷 + 인덱스
       ↓
[4.5/10] Bedrock Knowledge Base 생성
       • S3 Vectors 연결
       • S3 데이터 소스 (docs/) 연결
       ↓
[5/10] VPC 네트워킹 리소스 생성
       • VPC, 서브넷 생성
       • IGW, NAT Gateway 생성
       • 보안 그룹 생성 (ALB SG, ECS SG)
       • Bedrock Runtime VPC 엔드포인트 생성
       ↓
[6/10] Application Load Balancer 생성
       ↓
[7/10] CloudFront 배포 생성
       • OAI 생성
       • S3 버킷 정책 업데이트
       • ALB + S3 하이브리드 오리진
       ↓
[8/10] ECR 리포지토리 생성 및 Docker 이미지 push
       • Dockerfile 기반 linux/amd64 빌드
       • ecr-for-{project_name}:latest push
       ↓
[9/10] ECS Fargate 서비스 배포
       • CloudWatch Logs 그룹 생성
       • IP 타겟 그룹 + ALB 리스너 연결
       • 태스크 정의 등록 및 서비스 생성
       • Private Subnet에 Fargate 태스크 실행
       ↓
[10/10] 애플리케이션 준비 상태 확인
       ↓
완료 - application/config.json 업데이트
```

---

## 배포 완료 후

배포가 완료되면 다음 정보가 출력됩니다:

```
================================================================
Infrastructure Deployment Completed Successfully!
================================================================
Summary:
  S3 Bucket: storage-for-sam-{account_id}-us-west-2
  VPC ID: vpc-xxxxxxxxx
  Public Subnets: subnet-xxx, subnet-yyy
  Private Subnets: subnet-aaa, subnet-bbb
  ALB DNS: http://alb-for-sam-xxxxxx.us-west-2.elb.amazonaws.com/
  CloudFront Domain: https://xxxxxxxxx.cloudfront.net
  ECS Service: service-for-sam (Fargate in private subnet)
  ECR Image: {account_id}.dkr.ecr.us-west-2.amazonaws.com/ecr-for-sam:latest
  S3 Vector Bucket: sam-{account_id}
  S3 Vector Index ARN: arn:aws:s3vectors:...
  Knowledge Base ID: XXXXXXXXXX
  Knowledge Base Role: arn:aws:iam::...
  AgentCore Memory Role: arn:aws:iam::...

Total deployment time: XX.XX minutes
================================================================
```

### application/config.json

배포 성공/실패와 관계없이 `finally` 블록에서 `application/config.json`이 갱신됩니다. 주요 필드:

| 필드 | 설명 |
|------|------|
| `projectName`, `accountId`, `region` | 프로젝트 기본 정보 |
| `knowledge_base_id`, `data_source_id` | Bedrock Knowledge Base |
| `knowledge_base_role`, `agentcore_memory_role` | IAM 역할 ARN |
| `vector_bucket_name`, `vector_bucket_arn` | S3 Vectors 버킷 |
| `vector_index_name`, `vector_index_arn` | S3 Vectors 인덱스 |
| `s3_bucket`, `s3_arn` | 문서 저장 S3 버킷 |
| `sharing_url` | CloudFront URL |
| `collectionArn`, `opensearch_url` | 레거시 호환용 빈 값 |

ECS 컨테이너에는 `APP_CONFIG_JSON` 환경변수로 동일한 설정이 주입되며, `docker-entrypoint.sh`가 시작 시 `application/config.json`으로 기록합니다.

### Docker Container 구성

애플리케이션은 Streamlit + Strands Agent 기반이며, 프로젝트 루트의 `Dockerfile`로 빌드됩니다.

```text
FROM --platform=linux/amd64 python:3.13-slim

WORKDIR /app

# Node.js (npx), curl (헬스체크)
RUN apt-get update && \
    apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip install streamlit==1.41.0 streamlit-chat streamlit-paste-button ...
# (기타 Python 패키지)

RUN mkdir -p /root/.streamlit
COPY config.toml /root/.streamlit/
COPY . .

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "-m", "streamlit", "run", "application/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

`docker-entrypoint.sh`는 `APP_CONFIG_JSON` 환경변수가 있으면 `/app/application/config.json`을 생성한 뒤 Streamlit을 실행합니다.

### 주의사항
- CloudFront 배포는 완전히 활성화되기까지 15-20분이 소요될 수 있습니다
- ECS Fargate 서비스가 안정화되고 ALB 헬스체크가 통과하기까지 수 분이 걸릴 수 있습니다
- `application/config.json` 파일이 자동으로 업데이트됩니다 (부분 배포 시에도 저장)
- Knowledge Base가 기존 OpenSearch Serverless를 사용 중이면 S3 Vectors로 마이그레이션 시 자동 삭제 후 재생성됩니다
- 기존 EC2 배포에서 생성된 `TG-for-{project_name}` 타겟 그룹이 `instance` 타입이면 ECS 배포 전 삭제가 필요합니다 (Fargate는 `ip` 타입 필요)
- Private Subnet의 Fargate 태스크는 NAT Gateway를 통해 ECR에서 이미지를 pull합니다

---

## 에러 처리

스크립트는 다음과 같은 에러를 자동으로 처리합니다:

| 상황 | 처리 방법 |
|------|----------|
| 리소스 이미 존재 | 기존 리소스 재사용 |
| 서브넷 부족 | 자동으로 서브넷 생성 |
| CIDR 충돌 | 대체 CIDR 블록 자동 선택 |
| 정책 이미 존재 | 기존 정책 업데이트 |
| KB 스토리지 불일치 | Knowledge Base 삭제 후 S3 Vectors로 재생성 |
| ECS 서비스 이미 존재 | 새 태스크 정의로 서비스 업데이트 (`forceNewDeployment`) |
| 타임아웃 | 재시도 로직 적용 |

배포 실패 시 상세한 에러 메시지와 스택 트레이스가 출력되며, 가능한 배포 정보는 `application/config.json`에 저장됩니다.

---

## 인프라 삭제

ECS/ECR 리소스를 포함한 전체 인프라 삭제:

```bash
python uninstaller.py
```

삭제 순서: CloudFront → ECS (서비스/클러스터/태스크 정의/로그/ECR) → ALB → EC2(레거시) → VPC → 기타 리소스
