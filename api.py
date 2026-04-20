import os
import re
from pathlib import Path
from typing import Any, Dict, Optional
import requests

from log_utils import get_logger


def _load_env_file():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file()

baseUrl: str = os.environ.get("XIAOZHI_BASE_URL", "http://127.0.0.1:8002/xiaozhi")
serverSecret: str = os.environ.get("XIAOZHI_SERVER_SECRET", "")
LOGGER = get_logger(__name__)


def _build_headers() -> Dict[str, str]:
    if not serverSecret:
        raise RuntimeError("XIAOZHI_SERVER_SECRET is not configured")
    return {
        "Authorization": f"Bearer {serverSecret}",
        "Content-Type": "application/json",
    }


def _sanitize_prompt_text(prompt: str) -> str:
    if not isinstance(prompt, str):
        prompt = str(prompt)
    prompt = prompt.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    prompt = prompt.replace("\\", " ")
    prompt = prompt.replace('"', " ")
    prompt = re.sub(r"[\x00-\x1f\x7f]", " ", prompt)
    prompt = re.sub(r"\s+", " ", prompt).strip()
    return prompt

def sendPrompt(deviceId: str, prompt: str):
    # print(f"已发送prompt: {prompt} to deviceId: {deviceId}")
    # return
    url = f"{baseUrl}/device/directChat/{deviceId}"
    headers = _build_headers()
    safe_prompt = _sanitize_prompt_text(prompt)

    data = {
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "self.direct_chat",
                "arguments": {
                    "message": safe_prompt
                },
            },
        },
    }
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        response_json = response.json()
        LOGGER.debug("Prompt sent. deviceId=%s prompt=%s response=%s", deviceId, safe_prompt, response_json)
        return {
            "ok": response_json.get("code") == 0,
            "status_code": response.status_code,
            "data": response_json,
        }
    else:
        LOGGER.error(
            "Failed to send prompt. deviceId=%s prompt=%s status_code=%s response=%s",
            deviceId,
            safe_prompt,
            response.status_code,
            response.text,
        )
        return {
            "ok": False,
            "status_code": response.status_code,
            "text": response.text,
        }

def getDeviceOnlineStatus(deviceId: str):
    url = f"{baseUrl}/device/online/{deviceId}"
    headers = _build_headers()
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        LOGGER.debug("Device online status retrieved. deviceId=%s response=%s", deviceId, response.json())
        return {
            "ok": True,
            "status_code": response.status_code,
            "data": response.json(),
        }
    else:
        LOGGER.error(
            "Failed to retrieve device online status. deviceId=%s status_code=%s response=%s",
            deviceId,
            response.status_code,
            response.text,
        )
        return {
            "ok": False,
            "status_code": response.status_code,
            "text": response.text,
        }

def getDeviceOwnerBenefit(deviceId: str):
    url = f"{baseUrl}/activation-code/device/{deviceId}/benefits"
    headers = _build_headers()
    LOGGER.debug("Retrieving device owner benefit. deviceId=%s url=%s", deviceId, url)
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        response_json = response.json()
        LOGGER.debug("Device owner benefit retrieved. deviceId=%s response=%s", deviceId, response_json)
        return {
            "ok": response_json.get("code") == 0,
            "status_code": response.status_code,
            "data": response_json,
        }
    else:
        LOGGER.error(
            "Failed to retrieve device owner benefit. deviceId=%s status_code=%s response=%s",
            deviceId,
            response.status_code,
            response.text,
        )
        return {
            "ok": False,
            "status_code": response.status_code,
            "text": response.text,
        }


def getDeviceOwner(deviceId: str):
    url = f"{baseUrl}/device/owner/{deviceId}"
    headers = _build_headers()
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        response_json = response.json()
        LOGGER.debug("Device owner retrieved. deviceId=%s response=%s", deviceId, response_json)
        return {
            "ok": response_json.get("code") == 0,
            "status_code": response.status_code,
            "data": response_json,
        }
    LOGGER.error(
        "Failed to retrieve device owner. deviceId=%s status_code=%s response=%s",
        deviceId,
        response.status_code,
        response.text,
    )
    return {
        "ok": False,
        "status_code": response.status_code,
        "text": response.text,
    }
    
def consumeDeviceOwnerBalance(deviceId: str, amount: int):
    url = f"{baseUrl}/activation-code/device/{deviceId}/balance/consume"
    headers = _build_headers()
    data = {
        "seconds": amount,
        "sourceType": "live_chat",
    }
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        response_json = response.json()
        LOGGER.debug("Device owner balance consumed. deviceId=%s amount=%s response=%s", deviceId, amount, response_json)
        return {
            "ok": response_json.get("code") == 0,
            "status_code": response.status_code,
            "data": response_json,
        }
    else:
        LOGGER.error(
            "Failed to consume device owner balance. deviceId=%s amount=%s status_code=%s response=%s",
            deviceId,
            amount,
            response.status_code,
            response.text,
        )
        return {
            "ok": False,
            "status_code": response.status_code,
            "text": response.text,
        }
