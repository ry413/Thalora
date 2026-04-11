import os
from pathlib import Path
from typing import Any, Dict, Optional
import requests


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


def _build_headers() -> Dict[str, str]:
    if not serverSecret:
        raise RuntimeError("XIAOZHI_SERVER_SECRET is not configured")
    return {
        "Authorization": f"Bearer {serverSecret}",
        "Content-Type": "application/json",
    }

def sendPrompt(deviceId: str, prompt: str):
    # print(f"已发送prompt: {prompt} to deviceId: {deviceId}")
    # return
    url = f"{baseUrl}/device/directChat/{deviceId}"
    headers = _build_headers()

    data = {
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "self.direct_chat",
                "arguments": {
                    "message": prompt
                },
            },
        },
    }
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        print(f"Prompt sent successfully. Response: {response.json()}")
        return {
            "ok": True,
            "status_code": response.status_code,
            "data": response.json(),
        }
    else:
        print(f"Failed to send prompt. Status code: {response.status_code}, Response: {response.text}")
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
        print(f"Device online status retrieved successfully. Response: {response.json()}")
        return {
            "ok": True,
            "status_code": response.status_code,
            "data": response.json(),
        }
    else:
        print(f"Failed to retrieve device online status. Status code: {response.status_code}, Response: {response.text}")
        return {
            "ok": False,
            "status_code": response.status_code,
            "text": response.text,
        }

def getDeviceOwnerBenefit(deviceId: str):
    url = f"{baseUrl}/activation-code/device/{deviceId}/benefits"
    headers = _build_headers()
    print(f"Attempting to retrieve device owner benefit for deviceId: {deviceId} from URL: {url}")
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        response_json = response.json()
        print(f"Device owner benefit retrieved successfully. Response: {response_json}")
        return {
            "ok": response_json.get("code") == 0,
            "status_code": response.status_code,
            "data": response_json,
        }
    else:
        print(f"Failed to retrieve device owner benefit. Status code: {response.status_code}, Response: {response.text}")
        return {
            "ok": False,
            "status_code": response.status_code,
            "text": response.text,
        }


def getDeviceOwnerId(deviceId: str):
    url = f"{baseUrl}/device/user/{deviceId}"
    headers = _build_headers()
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        response_json = response.json()
        print(f"Device owner id retrieved successfully. Response: {response_json}")
        return {
            "ok": response_json.get("code") == 0,
            "status_code": response.status_code,
            "data": response_json,
        }
    print(f"Failed to retrieve device owner id. Status code: {response.status_code}, Response: {response.text}")
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
        print(f"Device owner balance consumed successfully. Response: {response_json}")
        return {
            "ok": response_json.get("code") == 0,
            "status_code": response.status_code,
            "data": response_json,
        }
    else:
        print(f"Failed to consume device owner balance. Status code: {response.status_code}, Response: {response.text}")
        return {
            "ok": False,
            "status_code": response.status_code,
            "text": response.text,
        }
