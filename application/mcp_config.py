import logging
import sys
import utils
import os
import boto3

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("mcp-config")

config = utils.load_config()
logger.info(f"config: {config}")

region = config["region"] if "region" in config else "us-west-2"
projectName = config["projectName"] if "projectName" in config else "mcp"
workingDir = os.path.dirname(os.path.abspath(__file__))
logger.info(f"workingDir: {workingDir}")

mcp_user_config = {}


def get_agentcore_gateway_mcp_url(gateway_name: str, gateway_region: str) -> str | None:
    client = boto3.client("bedrock-agentcore-control", region_name=gateway_region)
    try:
        response = client.list_gateways()
        for item in response.get("items", []):
            if item.get("name") != gateway_name:
                continue

            gateway_id = item["gatewayId"]
            gateway = client.get_gateway(gatewayIdentifier=gateway_id)
            return gateway["gatewayUrl"].rstrip("/")
    except Exception as e:
        logger.error(f"Error resolving AgentCore gateway URL for {gateway_name}: {e}")

    return None


def get_websearch_gateway_url() -> str | None:
    """Prefer installer-provided config.json URL, then resolve via AgentCore Control API."""
    configured_url = (config.get("agentcore_websearch_gateway_url") or "").strip().rstrip("/")
    if configured_url:
        return configured_url

    gateway_name = config.get("agentcore_websearch_gateway_name", "gateway-websearch")
    gateway_region = config.get("agentcore_websearch_gateway_region", "us-east-1")
    return get_agentcore_gateway_mcp_url(gateway_name, gateway_region)


def load_config(mcp_type):
    if mcp_type == "aws documentation":
        mcp_type = 'aws_documentation'
    elif mcp_type == "short term memory":
        mcp_type = "short-term-memory"
    elif mcp_type == "long term memory":
        mcp_type = "long-term-memory"

    if mcp_type == "aws_documentation":
        return {
            "mcpServers": {
                "awslabs.aws-documentation-mcp-server": {
                    "command": "uvx",
                    "args": ["awslabs.aws-documentation-mcp-server@latest"],
                    "env": {
                        "FASTMCP_LOG_LEVEL": "ERROR"
                    }
                }
            }
        }
    
    elif mcp_type == "RAG":
        return {
            "mcpServers": {
                "retrieve": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_retrieve.py"]
                }
            }
        }

    elif mcp_type == "web_fetch":
        return {
            "mcpServers": {
                "web_fetch": {
                    "command": "npx",
                    "args": ["-y", "mcp-server-fetch-typescript"]
                }
            }
        }  
    
    elif mcp_type == "trade_info":
        return {
            "mcpServers": {
                "trade_info": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_trade_info.py"
                    ]
                }
            }
        }        

    elif mcp_type == "korea_weather":
        return {
            "mcpServers": {
                "korea-weather": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_korea_weather.py"]
                }
            }
        }

    elif mcp_type == "short-term-memory":
        return {
            "mcpServers": {
                "short-term memory": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_short_term_memory.py"]
                }
            }
        }

    elif mcp_type == "long-term-memory":
        return {
            "mcpServers": {
                "long-term memory": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_long_term_memory.py"]
                }
            }
        }

    elif mcp_type == "websearch":
        gateway_url = get_websearch_gateway_url()
        if not gateway_url:
            logger.info(
                "AgentCore gateway websearch MCP skipped: "
                "gateway-websearch not found in us-east-1."
            )
            return {}
        gateway_region = config.get("agentcore_websearch_gateway_region", "us-east-1")
        return {
            "mcpServers": {
                "gateway-websearch": {
                    "type": "streamable_http",
                    "url": gateway_url,
                    "auth_type": "aws_sigv4",
                    "auth_region": gateway_region,
                    "auth_service": "bedrock-agentcore",
                }
            }
        }

    elif mcp_type == "사용자 설정":
        return mcp_user_config


def load_selected_config(mcp_servers: dict):
    logger.info(f"mcp_servers: {mcp_servers}")
    
    loaded_config = {}
    for server in mcp_servers:
        config = load_config(server)        
        if config:
            loaded_config.update(config["mcpServers"])
    return {
        "mcpServers": loaded_config
    }
