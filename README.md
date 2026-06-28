# Strands ECS Agent

Amazon ECS에서 [Strands agent](https://strandsagents.com/0.1.x/)를 이용해 Agentic AI를 구현하는 방법을 설명합니다. Strands Agent는 AI agent 구축 및 실행을 위해 설계된 오픈소스 SDK입니다. 계획(planning), 사고 연결(chaining thoughts), 도구 호출, Reflection과 같은 agent 기능을 쉽게 활용할 수 있습니다.

Agent의 기본 동작 확인 및 구현을 위해 **ECS Fargate**에 Docker 컨테이너 형태로 탑재되어 ALB와 CloudFront를 이용해 Streamlit으로 테스트할 수 있습니다. `installer.py`가 AgentCore Memory·**AgentCore Web Search Gateway**·Bedrock Knowledge Base (S3 Vectors)·**S3 Files** 세션 스토리지·ECS 인프라를 자동 배포합니다. User ID별로 대화·메모리를 분리하며, MCP(`short term memory`, `long term memory`, `websearch` 등)와 **Agent Skills**로 에이전트 기능을 확장합니다.

<img width="1000" alt="image" src="https://github.com/user-attachments/assets/2c1439a4-b9ad-4f1b-874e-53856a913fa4" />


## Session Management & S3 Files

ECS Fargate 컨테이너 안에서 Strands Agent가 **대화 이력·agent state**를 유지하기 위해 **Amazon S3 Files**를 `/mnt/workspace`에 마운트하고, Strands **`FileSessionManager`**가 해당 경로에 세션을 저장합니다. AgentCore Memory(MCP)와 별개로, Strands SDK의 **session storage**가 Agent 대화 컨텍스트를 영속화합니다.

상세 동작은 [strands-runtime/session-management.md](../strands-runtime/session-management.md)와 동일한 Strands SDK 패턴을 따르며, 이 프로젝트는 **AgentCore Runtime 대신 ECS 태스크**에 S3 Files volume을 붙입니다.

### 한 줄 요약

| 매니저 | 역할 | 저장 위치 |
|---|---|---|
| `conversation_manager` | 모델에 보낼 메시지 **개수/크기 제한** (슬라이딩 윈도우) | 프로세스 메모리 (상태는 session에도 동기화) |
| `session_manager` | 전체 대화·agent state **디스크 저장/복원** | `/mnt/workspace/session_<id>/...` (S3 Files → S3 bucket) |

```
[전체 대화 히스토리]  ← session_manager가 S3 Files(/mnt/workspace)에 저장
        ↓
[슬라이딩 윈도우 50 messages] ← conversation_manager가 모델에 전달할 부분만 선택
        ↓
      LLM 호출
```

### Session ID (Streamlit 로그인)

[strands-runtime/application/app.py](../strands-runtime/application/app.py)와 같이 User ID 로그인 후 **Session ID**를 관리합니다.

| 단계 | 동작 |
|---|---|
| **로그인** | User ID 입력 → `chat.set_user_id()` → `runtime_session_id = uuid5("agentcore-session-{user_id}")` (재접속 시 동일) |
| **대화 초기화** | `chat.initiate()` → 새 `runtime_session_id`(uuid4) → Agent 재생성 |
| **Agent** | `FileSessionManager(session_id=chat.runtime_session_id, storage_dir="/mnt/workspace")` |

[app.py](./application/app.py) 사이드바에 **User ID**, **Session ID**가 표시됩니다. [chat.py](./application/chat.py)의 `runtime_session_id_for()`는 strands-runtime [agentcore_client.py](../strands-runtime/application/agentcore_client.py)와 동일한 deterministic UUID 규칙을 사용합니다.

### 코드 구조 ([strands_agent.py](./application/strands_agent.py))

**conversation_manager** — 모듈 레벨 싱글톤, `window_size=50` (**메시지 개수** 기준, turn 수 아님)

```python
conversation_manager = SlidingWindowConversationManager(window_size=50)
```

**session_manager** — `create_agent()`마다 생성

```python
from strands.session.file_session_manager import FileSessionManager

session_manager = FileSessionManager(
    session_id=get_runtime_session_id(),  # chat.runtime_session_id
    storage_dir=get_session_storage_dir(),  # /mnt/workspace
)

agent = Agent(
    model=model,
    system_prompt=BASE_SYSTEM_PROMPT,
    tools=tools,
    plugins=[skills_plugin] if skills_plugin else [],
    conversation_manager=conversation_manager,
    session_manager=session_manager,
)
```

`run_strands_agent()`는 tool/MCP/skill 설정 또는 **session_id 변경** 시 Agent를 재생성하고, `session_manager.initialize()`로 디스크에서 대화를 복원합니다.

#### window_size 참고

| 흐름 | `agent.messages`에 추가되는 메시지 |
|---|---|
| `request → response` (tool 없음) | **2** (`user` + `assistant`) |
| `request → toolUse → toolResult → response` | **4** |

디스크에는 전체 대화가 저장되고, 모델에는 최근 **50개 메시지**만 전달됩니다.

### 디스크 저장 구조

```
/mnt/workspace/
└── session_<session_id>/
    ├── session.json
    └── agents/
        └── agent_<agent_id>/
            ├── agent.json          # state, conversation_manager_state 등
            └── messages/
                ├── message_0.json
                └── ...
```

S3 측 동기화 경로 (버킷 prefix `agentcore-sessions/`):

```text
s3://storage-for-{project_name}-{account_id}-{region}/
  agentcore-sessions/
    session_<session_id>/
      session.json
      agents/agent_default/...
```

### S3 Files on ECS Fargate

[strands-runtime](../strands-runtime)은 AgentCore Runtime에 S3 Files를 마운트합니다. **이 프로젝트**는 동일한 S3 Files 인프라를 프로비저닝하되, **ECS Fargate 태스크 정의**의 `s3filesVolumeConfiguration`으로 컨테이너에 마운트합니다.

```mermaid
flowchart TB
    subgraph ecs ["ECS Fargate (private subnet)"]
        APP["Streamlit + strands_agent.py"]
        FM["FileSessionManager\n/mnt/workspace"]
        APP --> FM
    end

    subgraph nfs ["S3 Files (NFS 2049)"]
        AP[Access Point]
        MT[Mount Targets]
        FS[File System]
        AP --> MT --> FS
    end

    subgraph s3 ["S3 Bucket"]
        PREFIX["agentcore-sessions/\nsession_&lt;id&gt;/..."]
    end

    FM -->|파일 I/O| AP
    FS -->|비동기 동기화| PREFIX
```

| 항목 | strands-runtime (AgentCore) | strands-ecs-project (ECS) |
|---|---|---|
| 마운트 방식 | `filesystemConfigurations.s3FilesAccessPoint` | ECS task `s3filesVolumeConfiguration` |
| 마운트 경로 | `/mnt/workspace` | `/mnt/workspace` (동일) |
| session_id | `BedrockAgentCoreContext.get_session_id()` | `chat.runtime_session_id` (User ID 기반) |
| IAM | Runtime 실행 역할 | **ECS task role** |
| 네트워크 | Runtime VPC + SG(2049) | ECS task SG + mount target SG(2049) |

#### installer 프로비저닝 (`[5.5/10]`)

[installer.py](./installer.py)의 `create_s3_files_session_storage()`가 **멱등**으로 생성합니다.

1. **Sync IAM role** — `role-s3files-sync-for-{project_name}`
2. **S3 bucket versioning** — `Enabled` (S3 Files 필수)
3. **File system** — bucket + prefix `agentcore-sessions/`
4. **Security groups** — ECS SG ↔ mount target SG (TCP **2049**)
5. **Mount targets** — private subnet별
6. **Access point** — POSIX `uid/gid: 0/0`
7. **File system policy** — ECS task role에 NFS mount/write 허용
8. **ECS task definition** — volume + `mountPoints` → `/mnt/workspace`

`application/config.json`에 기록되는 키:

| 키 | 설명 |
|---|---|
| `s3_files_file_system_id` | S3 Files file system ID |
| `s3_files_access_point_arn` | Access point ARN |
| `s3_files_mount_path` | `/mnt/workspace` |
| `ecs_session_vpc_subnets` | ECS 태스크 subnet 목록 |
| `ecs_session_security_groups` | ECS task security group |
| `agentcore_websearch_gateway_name` | Web Search Gateway 이름 (`gateway-websearch`) |
| `agentcore_websearch_gateway_region` | Gateway 리전 (`us-east-1`) |
| `agentcore_websearch_gateway_id` | Gateway ID |
| `agentcore_websearch_gateway_url` | MCP Streamable HTTP URL (`.../mcp`) |
| `agentcore_websearch_gateway_role` | Gateway 서비스 IAM role ARN |
| `default_skills` | Streamlit 기본 Skill 선택 |
| `default_strands_tool_selections` | Streamlit 기본 Strands Tool 선택 |

ECS 태스크 정의 예 ([installer.py](./installer.py)):

```python
"volumes": [{
    "name": "session-storage",
    "s3filesVolumeConfiguration": {
        "fileSystemArn": file_system_arn,
        "accessPointArn": access_point_arn,
        "rootDirectory": "/",
    },
}],
"mountPoints": [{
    "sourceVolume": "session-storage",
    "containerPath": "/mnt/workspace",
    "readOnly": False,
}],
```

#### ECS task role IAM (S3 Files)

`attach_ecs_task_s3files_policy()`가 task role에 아래 권한을 추가합니다.

- `s3files:ClientMount`, `ClientWrite`, `ClientRootAccess` (file system ARN + access point 조건)
- `s3files:GetAccessPoint` (access point ARN)
- `s3files:ListMountTargets` (file system ARN)

#### 재시작·배포 시 동작

| 시나리오 | 동작 |
|---|---|
| **같은 User ID로 재접속** | deterministic session id → S3 Files에서 대화 복원 |
| **대화 초기화** | 새 session id → 새 `session_<id>/` 디렉터리 |
| **ECS 태스크 재시작** | `/mnt/workspace`는 S3 Files volume → 세션 유지 |
| **새 Docker 이미지 배포** | 동일 volume 마운트 → 세션 유지 |

> S3 Files는 NFS 기반이므로 S3 API로 즉시 읽을 때 **동기화 지연(~60초)** 이 있을 수 있습니다. `FileSessionManager`만 사용하는 Agent 세션에는 일반적으로 문제 없습니다.

#### 주의사항

- `session_id`는 User ID·대화 초기화 단위로 고유해야 합니다.
- `/mnt/workspace`는 ECS task에 S3 Files volume이 마운트되어 있어야 합니다. 로컬 `streamlit run` 시에는 마운트가 없으므로 세션 영속화는 ECS 배포 환경을 기준으로 합니다.
- mount target AZ·ECS task subnet·SG(2049)가 맞지 않으면 태스크 기동 또는 파일 I/O가 실패할 수 있습니다.
- 세션 파일은 버킷 루트가 아니라 **`agentcore-sessions/`** prefix 아래에 동기화됩니다.

관련 문서: [strands-runtime/session-management.md](../strands-runtime/session-management.md), [strands-runtime/s3files.md](../strands-runtime/s3files.md)


Strands agent는 아래와 같은 [Agent Loop](https://strandsagents.com/0.1.x/user-guide/concepts/agents/agent-loop/)을 가지고 있으므로, 적절한 tool을 선택하여 실행하고, reasoning을 통해 반복적으로 필요한 동작을 수행합니다. 

![image](https://github.com/user-attachments/assets/6f641574-9d0b-4542-b87f-98d7c2715e09)

## Strands Agent 활용 방법

### Operation Architecture

```mermaid
flowchart TB
  subgraph UI["Streamlit (app.py)"]
    UID[User ID 입력]
    MEMCHK[Memory on/off]
    M[Agent]
    SKUI[Skill / Strands Tool / MCP 선택]
  end

  subgraph MemoryStack["AgentCore Memory"]
    STM[Short-term: create_event / list_events]
    LTM[Long-term: strategy 추출 / retrieve]
    UJSON["user_{user_id}.json"]
  end

  subgraph LLM["Amazon Bedrock"]
    BR[Bedrock Runtime]
  end

  subgraph Skills["Agent Skills (application/skills/)"]
    SRC["skills/*/SKILL.md"]
    ASK[AgentSkills plugin]
    GSI[get_skill_instructions]
  end

  subgraph StrandsStack["Strands Agents SDK (strands_agent.py)"]
    RSA[run_strands_agent]
    A[Agent]
    SA[stream_async]
    BM[BedrockModel]
    BT["Built-in: execute_code, bash, upload_file_to_s3"]
    ST["strands_tools: current_time, file_read, file_write"]
    MCP[MCPClient / MCPClientManager]
  end

  subgraph MCPServers["MCP Servers (mcp_config.py)"]
    STMCP[short-term memory]
    LTMCP[long-term memory]
    R[retrieve / RAG]
    AWS[aws documentation]
    WF[web_fetch / korea_weather / trade_info]
    WS_MCP[websearch\nAgentCore Gateway + SigV4]
  end

  subgraph Storage["Artifacts / S3 / ECS / S3 Files"]
    ART[artifacts/]
    S3[(S3 bucket)]
    S3F["S3 Files\nagentcore-sessions/"]
    WS["/mnt/workspace\nFileSessionManager"]
    ECS[ECS Fargate]
  end

  UID --> RSA
  MEMCHK -->|save_to_memory| STM
  M --> RSA
  SKUI -->|skill_list| ASK
  SKUI -->|mcp_servers| MCP

  RSA --> A
  A --> SA
  A --> BM
  BM --> BR
  A --> BT
  A --> ST
  A --> MCP
  A --> GSI
  ASK -->|plugin tools| A
  GSI --> SRC
  MCP --> MCPServers
  MCP --> STMCP
  MCP --> LTMCP
  MCP --> WS_MCP
  STMCP --> STM
  LTMCP --> LTM
  WS_MCP -->|SigV4 InvokeGateway| ECS
  STM --> UJSON
  LTM --> UJSON
  BT --> ART
  BT --> S3
  A --> WS
  WS --> S3F
  S3F --> S3
```

| 모드 | 모듈 | 설명 |
|------|------|------|
| 일상적인 대화 | `chat.general_conversation` | 대화 이력 + Bedrock Runtime `invoke_model_with_response_stream` 스트리밍 |
| RAG | `chat.run_rag_with_knowledge_base` | Bedrock Knowledge Base 검색(`retrieve`) 후 Bedrock Runtime으로 답변 생성 |
| **Agent** | `strands_agent.run_strands_agent` | Strands SDK + built-in tools + strands_tools + MCP + Agent Skills |
| 이미지 분석 | `chat.summarize_image` | ChatBedrock 멀티모달 (이미지 + 텍스트) 분석 |

### Agent 모드 UI 구성 ([app.py](./application/app.py))

| 구분 | 선택 가능 항목 | 기본값 (`config.json`) |
|------|----------------|------------------------|
| **Skill** | `application/skills/*/SKILL.md` | `skill-creator`, `docx` |
| **Strands Tool** | `current_time`, `file_read`, `file_write`, `http_request` | `current_time`, `file_read`, `file_write` |
| **MCP** | `RAG`, `aws documentation`, `trade_info`, `web_fetch`, `websearch`, `korea_weather`, `short term memory`, `long term memory`, `사용자 설정` | `korea_weather`, `web_fetch`, `websearch`, `long term memory` |

> Tavily MCP는 UI 기본 목록에서 제거되었습니다. 웹 검색은 **AgentCore Web Search Gateway** MCP(`websearch`)를 사용합니다.


### Streamlit에서 agent의 실행

[app.py](./application/app.py)와 같이 사용자가 "RAG", "Agent"을 선택할 수 있습니다. "Agent"은 Strands agent를 이용하여 MCP로 필요시 tool들을 이용하여 RAG등을 활용할 수 있습니다. Streamlit의 UI를 위하여 user의 입력과 결과인 response을 [Session State](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.session_state)로 관리합니다. 

```python
if prompt := st.chat_input("메시지를 입력하세요."):
    with st.chat_message("user"):  
        st.markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    with st.status("thinking...", expanded=True, state="running") as status:
        notification_queue = NotificationQueue(container=status)
        skill_list = selected_skills if selected_skills else []

        response, image_urls = asyncio.run(strands_agent.run_strands_agent(
            query=prompt, 
            strands_tools=selected_strands_tools, 
            mcp_servers=selected_mcp_servers, 
            skill_list=skill_list,
            notification_queue=notification_queue))
```

### Agent의 실행

[strands_agent.py](./application/strands_agent.py)에서 Agent는 아래 순서로 구성됩니다.

1. **`_warm_aws_credentials()`** — ECS task role credential 선로드 (Web Search Gateway SigV4용)
2. **`init_mcp_clients()`** — 선택된 MCP 서버 설정 등록
3. **`update_tools()`** — built-in tools + strands_tools + MCP tools 수집
4. **`AgentSkills` plugin** — Skill 모드가 Enable이면 `application/skills/` 로드
5. **`FileSessionManager`** — S3 Files(`/mnt/workspace`)에 세션 영속화

```python
def create_agent(strands_tools, mcp_servers, skill_list):
    _warm_aws_credentials()
    init_mcp_clients(mcp_servers)
    tools = update_tools(strands_tools, mcp_servers)

    skills_plugin = None
    if chat.skill_mode == "Enable" and skill_list:
        skills_plugin = AgentSkills(skills=skill_dirs_from_list(skill_list))

    session_manager = FileSessionManager(
        session_id=get_runtime_session_id(),
        storage_dir=get_session_storage_dir(),
    )

    return Agent(
        model=get_model(),
        system_prompt=BASE_SYSTEM_PROMPT,
        tools=tools,
        plugins=[skills_plugin] if skills_plugin else [],
        conversation_manager=conversation_manager,
        session_manager=session_manager,
    )
```

**Built-in tools** (`get_builtin_tools()`): `execute_code`, `bash`, `upload_file_to_s3` — AgentSkills와 함께 사용합니다.

**Strands tools** (UI에서 선택): `current_time`, `file_read`, `file_write`, `http_request`

`run_strands_agent()`는 tool/MCP/skill/session 설정이 바뀌면 Agent를 재생성하고, `start_agent_clients()`로 MCP 세션을 유지합니다.

```python
async def run_strands_agent(query, strands_tools, mcp_servers, skill_list, notification_queue):
    # 설정 변경 시 Agent 재생성 + persistent MCP clients 시작
    agent = create_agent(strands_tools, mcp_servers, skill_list)
    mcp_manager.start_agent_clients(mcp_servers)

    with mcp_manager.get_active_clients(mcp_servers):
        async for event in agent.stream_async(query):
            if "data" in event:
                ...
            elif "current_tool_use" in event:
                ...
            elif "result" in event:
                ...
    return final_result, image_urls
```

### 대화 이력의 활용

Strands Agent는 **두 계층**으로 대화를 관리합니다. ([Session Management & S3 Files](#session-management--s3-files) 참조)

1. **`FileSessionManager`** — 전체 대화를 `/mnt/workspace`(S3 Files)에 영속 저장·복원
2. **`SlidingWindowConversationManager`** — 모델에 전달할 최근 메시지만 in-memory trim

[application/strands_agent.py](./application/strands_agent.py):

```python
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.session.file_session_manager import FileSessionManager

conversation_manager = SlidingWindowConversationManager(window_size=50)

session_manager = FileSessionManager(
    session_id=get_runtime_session_id(),
    storage_dir=get_session_storage_dir(),
)

agent = Agent(
    model=model,
    system_prompt=BASE_SYSTEM_PROMPT,
    tools=tools,
    conversation_manager=conversation_manager,
    session_manager=session_manager,
)
```

`window_size=50`은 **메시지 50개** 기준입니다 (tool 1회 포함 요청 ≈ 4 messages).

### MCP 활용

[mcp_config.py](./application/mcp_config.py)가 MCP 유형별 설정을 반환하고, [strands_agent.py](./application/strands_agent.py)의 `MCPClientManager`가 Strands `MCPClient`로 연결합니다.

| MCP 이름 | transport | 설명 |
|----------|-----------|------|
| `RAG` | stdio | Bedrock Knowledge Base retrieve (`mcp_server_retrieve.py`) |
| `aws documentation` | stdio | `uvx awslabs.aws-documentation-mcp-server@latest` |
| `web_fetch` | stdio | `npx mcp-server-fetch-typescript` |
| `korea_weather` | stdio | 기상청 API (`mcp_server_korea_weather.py`) |
| `trade_info` | stdio | 무역 정보 (`mcp_server_trade_info.py`) |
| `short term memory` | stdio | AgentCore Memory short-term (`mcp_server_short_term_memory.py`) |
| `long term memory` | stdio | AgentCore Memory long-term (`mcp_server_long_term_memory.py`) |
| **`websearch`** | **streamable_http** | AgentCore Web Search Gateway (`gateway-websearch`, `us-east-1`) |
| `사용자 설정` | — | `mcp_user_config`에 사용자 정의 MCP JSON |

#### AgentCore Web Search (`websearch`)

`installer.py`가 `us-east-1`에 `gateway-websearch` Gateway를 생성(또는 기존 Gateway 재사용)하고, URL·ID를 `application/config.json`에 기록합니다. ECS task role이 Gateway MCP URL에 **SigV4**(`bedrock-agentcore`)로 인증합니다.

```python
# mcp_config.py
"gateway-websearch": {
    "type": "streamable_http",
    "url": gateway_url,  # config.json agentcore_websearch_gateway_url
    "auth_type": "aws_sigv4",
    "auth_region": "us-east-1",
    "auth_service": "bedrock-agentcore",
}
```

[agentcore_sigv4_auth.py](./application/agentcore_sigv4_auth.py)는 LangGraph ECS 프로젝트와 동일한 httpx `Auth` 구현을 사용합니다. SigV4 auth와 httpx client는 MCP **백그라운드 스레드** 안에서 생성되어 ECS task role credential이 요청 스레드와 일치합니다.

#### MCPClientManager

stdio MCP와 streamable HTTP MCP(websearch)를 lazy initialization합니다. Agent 실행 중에는 `start_agent_clients()`로 persistent session을 유지합니다.

```python
class MCPClientManager:
    def add_stdio_client(self, name, command, args, env={}): ...
    def add_streamable_client(self, name, url, headers={}, auth_region=None, auth_service=None): ...
    def get_client(self, name) -> MCPClient: ...  # lazy create
    def start_agent_clients(self, client_names) -> bool: ...  # persistent MCP sessions
    def get_active_clients(self, active_clients): ...  # context manager
```

streamable HTTP + SigV4 예시 (websearch):

```python
# auth/httpx는 transport factory 내부(백그라운드 스레드)에서 생성
MCPClient(
    lambda u=url, r=auth_region, s=auth_service: _streamable_http_with_auth(u, r, s)
)
```

tool 목록은 `update_tools()`에서 MCP별로 `list_tools_sync()`로 수집합니다.

```python
for mcp_tool in mcp_servers:
    with mcp_manager.get_active_clients([mcp_tool]):
        client = mcp_manager.get_client(mcp_tool)
        tools.extend(client.list_tools_sync())
```

Agent 실행 시 persistent MCP clients를 재사용합니다.

```python
with mcp_manager.get_active_clients(mcp_servers):
    async for event in agent.stream_async(query):
        ...
```

#### Agent Skills

Skill은 `application/skills/<name>/SKILL.md` 형식입니다. Streamlit Agent 모드에서 Skill을 선택하면 `AgentSkills` plugin이 `get_skill_instructions` 등 skill tool을 Agent에 등록합니다. 기본 Skill은 `config.json`의 `default_skills`로 설정합니다.

#### 커스텀 MCP 서버 예 (Wikipedia)

stdio 기반 커스텀 MCP 서버 패턴은 [mcp_server_wikipedia.py](./application/mcp_server_wikipedia.py)를 참조합니다. UI의 **사용자 설정** MCP에 JSON으로 등록해 사용할 수 있습니다.


### Streamlit에 맞게 출력문 조정하기

Agent를 아래와 같이 실행하여 agent_stream을 얻습니다.

```python
with mcp_manager.get_active_clients(mcp_servers) as _:
    agent_stream = agent.stream_async(question)
```

사용자 경험을 위해서는 stream형태로 출력을 얻을 수 있어야 합니다. 이는 아래와 같이 agent_stream에서 event를 꺼낸후 "data"에서 추출하여 아래와 같이 current_response에 stream 결과를 모아서 보여줍니다.

```python
async for event in agent_stream:
    if "data" in event:
        text_data = event["data"]
        current_response += text_data

        containers["notification"][index].markdown(current_response)
```

Strands agent는 multi step reasoning을 통해 여러번 결과가 나옵니다. 최종 결과를 얻기 위해 아래와 같이 message의 content에서 text를 추출하여 마지막만을 추출합니다. 또한 tool마다 reference가 다르므로 아래와 같이 tool content의 text에서 reference를 추출합니다.  

```python
if "message" in event:
    message = event["message"]
    for msg_content in message["content"]:                
        result = msg_content["text"]
        current_response = ""

        tool_content = msg_content["toolResult"]["content"]
        for content in tool_content:
            content, urls, refs = get_tool_info(tool_name, content["text"])
            if refs:
                for r in refs:
                    references.append(r)
```

generate_image_with_colors라는 tool의 최종 이미지 경로는 아래와 같이 event_loop_metrics에서 추출합하여 image_urls로 활용합니다.

```python
if "event_loop_metrics" in event and \
    hasattr(event["event_loop_metrics"], "tool_metrics") and \
    "generate_image_with_colors" in event["event_loop_metrics"].tool_metrics:
    tool_info = event["event_loop_metrics"].tool_metrics["generate_image_with_colors"].tool
    if "input" in tool_info and "filename" in tool_info["input"]:
        fname = tool_info["input"]["filename"]
        if fname:
            url = f"{path}/{s3_image_prefix}/{parse.quote(fname)}.png"
            if url not in image_urls:
                image_urls.append(url)
```


## 배포하기

### 사전 요구사항

`installer.py`는 AWS 인프라 생성과 함께 **로컬 Docker**로 컨테이너 이미지를 빌드하여 **ECR**에 push한 뒤 **ECS Fargate** 서비스를 배포합니다.

| 항목 | 설명 |
|------|------|
| Docker CLI | `Dockerfile` 기반 이미지 빌드 (`linux/amd64`) |
| AWS CLI | ECR 로그인 및 자격 증명 |
| boto3 / botocore | `>=1.43.32` (Dockerfile·Gateway SigV4 호환) |
| bedrock-agentcore | AgentCore Memory (`MemoryClient`) |

배포 시 생성되는 주요 리소스: ECR (`ecr-for-{project_name}`), ECS Cluster/Service (S3 Files volume `/mnt/workspace`), ALB, CloudFront, VPC, Bedrock Knowledge Base (S3 Vectors), **AgentCore Memory**, **AgentCore Web Search Gateway** (`us-east-1`), **S3 Files** (`agentcore-sessions/` prefix)

상세한 배포 흐름은 [installer.md](./installer.md)를 참조하세요. `installer.py` 주요 단계:

| 단계 | 내용 |
|------|------|
| `[1/10]` | S3 bucket |
| `[2/10]` | IAM roles, AgentCore Memory, **Web Search Gateway** (`us-east-1`) |
| `[4/10]` ~ `[4.5/10]` | S3 Vectors, Knowledge Base |
| `[5/10]` | VPC, NAT, subnets |
| `[5.5/10]` | **S3 Files** session storage (`/mnt/workspace`) |
| `[6/10]` ~ `[7/10]` | ALB, CloudFront |
| `[8/10]` | ECR build & push |
| `[9/10]` | ECS Fargate deploy |
| `[10/10]` | Application readiness check |


AWS console의 CloudShell을 접속하거나, Docker가 설치된 로컬 환경에서 아래와 같이 준비합니다.

```text
sudo yum install python3 python3-pip git docker -y   # CloudShell / Amazon Linux
pip install boto3
sudo systemctl start docker   # 로컬 Linux에서 Docker 데몬 실행
```

아래와 같이 git source를 가져옵니다.

```text
git clone https://github.com/kyopark2014/strands-ecs-project
```

`installer.py` 상단의 `project_name`, `region`, `git_name`을 확인한 뒤 설치를 시작합니다.

```text
cd strands-ecs-project && python3 installer.py
```

- 이미 ECR에 이미지가 있고 인프라만 갱신할 때: `python3 installer.py --skip-docker-build`

설치가 완료되면 아래와 같은 CloudFront URL로 접속하여 동작을 확인합니다.

<img width="500" alt="cloudfront_address" src="https://github.com/user-attachments/assets/7ab1a699-eefb-4b55-b214-23cbeeeb7249" />

컨테이너 설정(`application/config.json` 내용)은 ECS 태스크 환경변수 `APP_CONFIG_JSON`으로 주입되며, `docker-entrypoint.sh`가 컨테이너 시작 시 파일을 생성합니다.

인프라가 더 이상 필요 없을 때에는 `uninstaller.py`를 이용해 제거합니다. (ECS 서비스, ECR 리포지토리, ALB, VPC 등)

```text
python uninstaller.py
```


### 배포된 Application 업데이트 하기

애플리케이션 코드를 변경한 뒤 다시 배포하려면 **Docker 이미지를 재빌드**하고 ECS 서비스를 갱신합니다. 가장 간단한 방법은 `installer.py`를 다시 실행하는 것입니다.

```text
cd strands-ecs-project && python3 installer.py
```

`installer.py`는 ECR에 새 이미지를 push하고, ECS 서비스를 새 태스크 정의로 업데이트합니다(`forceNewDeployment`).

이미지 재빌드 없이 ECS만 재시작하려면 AWS CLI를 사용합니다. (`project_name` 기본값: `strands-ecs`)

```text
aws ecs update-service \
  --cluster cluster-for-strands-ecs \
  --service service-for-strands-ecs \
  --force-new-deployment \
  --region us-west-2
```

또는 [ECS Console](https://us-west-2.console.aws.amazon.com/ecs/v2/clusters)에서 `cluster-for-{project_name}` → `service-for-{project_name}` → **Update service** → **Force new deployment**를 선택합니다.

> 이전 EC2 배포 방식의 `update.sh` / Session Manager 접속은 ECS 배포에서는 사용하지 않습니다.

### Local에서 실행하기

AWS 환경을 잘 활용하기 위해서는 [AWS CLI를 설치](https://docs.aws.amazon.com/ko_kr/cli/v1/userguide/cli-chap-install.html)하여야 합니다. CloudShell 또는 ECS 배포 환경에서는 AWS CLI가 기본 제공되는 경우가 많습니다. 로컬에 설치할 때는 아래 명령어를 참조합니다.

```text
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" 
unzip awscliv2.zip
sudo ./aws/install
```

AWS credential을 아래와 같이 AWS CLI를 이용해 등록합니다.

```text
aws configure
```

설치하다가 발생하는 각종 문제는 [Kiro-cli](https://aws.amazon.com/ko/blogs/korea/kiro-general-availability/)를 이용해 빠르게 수정합니다. 아래와 같이 설치할 수 있지만, Windows에서는 [Kiro 설치](https://kiro.dev/downloads/)에서 다운로드 설치합니다. 실행시는 셀에서 "kiro-cli"라고 입력합니다. 

```python
curl -fsSL https://cli.kiro.dev/install | bash
```

venv로 환경을 구성하면 편리하게 패키지를 관리합니다. 아래와 같이 환경을 설정합니다.

```text
python -m venv .venv
source .venv/bin/activate
```

이후 다운로드 받은 github 폴더로 이동한 후에 아래와 같이 필요한 패키지를 추가로 설치 합니다.

```text
pip install -r requirements.txt
```

이후 아래와 같은 명령어로 streamlit을 실행합니다. 

```text
streamlit run application/app.py
```

로컬 실행 시 `/mnt/workspace` S3 Files 마운트가 없어 **FileSessionManager 세션 영속화**와 **websearch MCP**(ECS task role SigV4)는 ECS 배포 환경에서 검증하는 것을 권장합니다.



### 실행 결과


## Reference

[Strands Python Example](https://github.com/strands-agents/docs/tree/main/docs/examples/python)

[Strands Agents SDK](https://strandsagents.com/0.1.x/)

[Strands Agents Samples](https://github.com/strands-agents/samples/tree/main)

[Example Built-in Tools](https://strandsagents.com/0.1.x/user-guide/concepts/tools/example-tools-package/)

[Introducing Strands Agents, an Open Source AI Agents SDK](https://aws.amazon.com/ko/blogs/opensource/introducing-strands-agents-an-open-source-ai-agents-sdk/)

[use_aws.py](https://github.com/strands-agents/tools/blob/main/src/strands_tools/use_aws.py)

[Strands Agents와 오픈 소스 AI 에이전트 SDK 살펴보기](https://aws.amazon.com/ko/blogs/tech/introducing-strands-agents-an-open-source-ai-agents-sdk/)

[Drug Discovery Agent based on Amazon Bedrock](https://github.com/hsr87/drug-discovery-agent)

[Strands Agent - Swarm](https://strandsagents.com/latest/user-guide/concepts/multi-agent/swarm/)

[Strands Agent Streamlit Demo](https://github.com/NB3025/strands-streamlit-chat-demo)


[생성형 AI로 AWS 보안 점검 자동화하기: Q CLI에서 Strands Agents까지](https://catalog.us-east-1.prod.workshops.aws/workshops/89fc3def-0260-4fa7-91ce-623ad9a4d04a/ko-KR)

[AI Agent를 활용한 EKS 애플리케이션 및 인프라 트러블슈팅](https://catalog.us-east-1.prod.workshops.aws/workshops/bbd8a1df-c737-4f88-9d19-17bcecb7e712/ko-KR)

[Strands Agents 및 AgentCore와 함께하는 바이오·제약 연구 어시스턴트 구현하기](https://catalog.us-east-1.prod.workshops.aws/workshops/fe97ac91-ff75-4753-a269-af39e7c3d765/ko-KR)

[Strands Agents & Amazon Bedrock AgentCore 워크샵](https://github.com/hsr87/strands-agents-for-life-science)

[Agentic AI로 구현하는 리뷰 관리 자동화](https://catalog.us-east-1.prod.workshops.aws/workshops/59ea75b5-532c-4b57-982e-e58152ae5c46/ko-KR)

[Strands Agent Workshop (한국어)](https://github.com/chloe-kwak/strands-agent-workshop)

[Agentic AI Workshop: AI Fund Manager](https://catalog.us-east-1.prod.workshops.aws/workshops/a8702b51-fcf3-43b3-8d37-511ef1b38688/ko-KR)

[Agentic AI 펀드 매니저](https://github.com/ksgsslee/investment_advisor_strands)

[Workshop - Strands SDK와 AgentCore를 활용한 에이전틱 AI](https://catalog.workshops.aws/strands/ko-KR)
