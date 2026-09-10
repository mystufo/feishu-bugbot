from flask import Flask, request, jsonify
from lark_oapi import Client
from lark_oapi.core.token import AccessTokenType
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from lark_oapi.api.contact.v3 import GetUserRequest, ListUserRequest
import os
import json
import hmac
import hashlib
import requests
from urllib.parse import quote
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

app = Flask(__name__)
load_dotenv()

# 飞书配置
APP_ID = os.getenv("GITLAB_NOTIFY_FEISHU_APP_ID")
APP_SECRET = os.getenv("GITLAB_NOTIFY_FEISHU_APP_SECRET")

# GitLab配置
GITLAB_SECRET_TOKEN = os.getenv("GITLAB_SECRET_TOKEN")
GITLAB_API_URL = os.getenv("GITLAB_API_URL", "https://gitlab.com/api/v4")
GITLAB_PRIVATE_TOKEN = os.getenv("GITLAB_PRIVATE_TOKEN")

# 当 GitLab webhook 隐藏邮箱（显示为 [REDACTED]）时，用 "用户名@COMPANY_EMAIL_DOMAIN" 反查飞书用户
COMPANY_EMAIL_DOMAIN = os.getenv("COMPANY_EMAIL_DOMAIN", "")

# 初始化飞书客户端
client = Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .build()

def format_operation_time(raw: str) -> str:
    """
    把 GitLab Webhook 中的 UTC 时间转成北京时间字符串。

    新版 GitLab 为 ISO 8601（2026-09-06T08:55:07.039Z），旧版为 "2026-09-06 08:55:07 UTC"，
    两种都支持；无法解析时原样返回，不影响通知发送。
    """
    if not raw:
        return ""
    utc_time = None
    try:
        utc_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            utc_time = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            return raw
    if utc_time.tzinfo is None:
        utc_time = utc_time.replace(tzinfo=timezone.utc)
    return utc_time.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

def verify_gitlab_signature(payload: bytes, signature: str) -> bool:
    """验证 GitLab Webhook 签名"""
    if not GITLAB_SECRET_TOKEN:
        return True  # 如果没有配置密钥，跳过验证
    
    if not signature:
        print("Warning: No signature provided in request")
        return False  # 如果没有签名，拒绝请求
    
    expected_signature = hmac.new(
        GITLAB_SECRET_TOKEN.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected_signature, signature)

def get_user_by_email(email: str, username: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """通过邮箱获取飞书用户信息"""
    try:
        # 如果邮箱被隐藏，尝试使用用户名 + 公司邮箱域名构建邮箱
        if email == '[REDACTED]':
            if not username or not COMPANY_EMAIL_DOMAIN:
                print("Email is redacted and COMPANY_EMAIL_DOMAIN/username is not available")
                return None
            email = f"{username}@{COMPANY_EMAIL_DOMAIN}"
            print(f"Using constructed email: {email}")

        request = GetUserRequest.builder() \
            .user_id_type("email") \
            .user_id(email) \
            .build()
        
        response = client.contact.v3.user.get(request)
        
        if response.success():
            print(f"Found user by email: {email}")
            return response.data.user
        else:
            print(f"Failed to get user info: code={response.code}, msg={response.msg}")
            return None
    except Exception as e:
        print(f"Error getting user info: {str(e)}")
        return None

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """通过用户名获取飞书用户信息（按邮箱前缀匹配，自动翻页遍历全部用户）"""
    try:
        page_token = None
        while True:
            builder = ListUserRequest.builder().page_size(100)
            if page_token:
                builder = builder.page_token(page_token)
            response = client.contact.v3.user.list(builder.build())

            if not response.success():
                print(f"Failed to get user list: code={response.code}, msg={response.msg}")
                return None

            for user in response.data.items or []:
                # 检查邮箱用户名是否匹配
                if user.email and user.email.split('@')[0] == username:
                    return user

            if not response.data.has_more or not response.data.page_token:
                break
            page_token = response.data.page_token

        print(f"User not found: {username}")
        return None
    except Exception as e:
        print(f"Error getting user list: {str(e)}")
        return None

def get_gitlab_user_info(user_id: int) -> Optional[Dict[str, Any]]:
    """通过 GitLab API 获取用户信息"""
    try:
        url = f"{GITLAB_API_URL}/users/{user_id}"
        #print(f"Requesting GitLab API: {url}")

        # 最简单的请求方式
        response = requests.get(
            url,
            headers={'PRIVATE-TOKEN': GITLAB_PRIVATE_TOKEN},
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Failed to get GitLab user info: status={response.status_code}")
            return None
            
    except Exception as e:
        print(f"Error getting GitLab user info: {str(e)}")
        return None

def get_mr_participants(project_id: int, mr_iid: int) -> list:
    """获取 MR 的所有参与者"""
    try:
        url = f"{GITLAB_API_URL}/projects/{project_id}/merge_requests/{mr_iid}/participants"
        #print(f"Requesting MR participants: {url}")
        
        response = requests.get(
            url,
            headers={'PRIVATE-TOKEN': GITLAB_PRIVATE_TOKEN},
            verify=False
        )
        
        if response.status_code == 200:
            participants = response.json()
            #print(f"Found {len(participants)} participants")
            return participants
        else:
            print(f"Failed to get MR participants: status={response.status_code}")
            return []
            
    except Exception as e:
        print(f"Error getting MR participants: {str(e)}")
        return []

def send_mr_notification(user_id: str, mr_info: Dict[str, Any]) -> None:
    """发送 MR 通知消息"""
    if not user_id:
        print(f"Invalid user_id: {user_id}")
        return

    # 构建卡片消息的基本元素
    elements = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**标题：**\n{mr_info['title']}"
                    }
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**状态：**\n{mr_info['state']}"
                    }
                }
            ]
        },
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**作者：**\n{mr_info['author']}"
                    }
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**操作人：**\n{mr_info['operator']}"
                    }
                }
            ]
        },
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**操作时间：**\n{mr_info['operation_time']}"
                    }
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**参与者：**\n{mr_info['participants']}"
                    }
                }
            ]
        }
    ]

    # 只有当 description 不为空时才添加内容部分
    if mr_info['description']:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**内容：**\n{mr_info['description']}"
            }
        })

    # 添加分隔线和链接
    elements.extend([
        {
            "tag": "hr"
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**链接：**\n[{mr_info['url']}]({mr_info['url']})"
            }
        },
        {
            "tag": "hr"
        },
        {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": "查看详情"
            },
            "type": "primary",
            "url": mr_info['url']
        }
    ])

    # 构建卡片消息
    card_content = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"MR {mr_info['action']} 通知"
            },
            "template": "blue" if mr_info['state'] == 'opened' else "green"
        },
        "elements": elements
    }

    try:
        request = CreateMessageRequest.builder() \
            .receive_id_type("user_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(user_id)
                .msg_type("interactive")
                .content(json.dumps(card_content))
                .build()
            ) \
            .build()

        response = client.im.v1.chat.create(request)
        if not response.success():
            print(f"Failed to send message: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}")
            return
        #print(f"Successfully sent message to {user_id}")
    except Exception as e:
        print(f"Error sending message: {str(e)}")

def handle_mr_event(data: Dict[str, Any]) -> None:
    """处理 MR 事件"""
    if data.get('object_kind') == 'merge_request':
        handle_merge_request(data)
    elif data.get('object_kind') == 'note':
        handle_mr_comment(data)

def handle_merge_request(data: Dict[str, Any]) -> None:
    """处理 MR 事件"""
    mr = data.get('object_attributes', {})
    operator = data.get('user', {})  # 操作人信息
    
    # 获取操作时间
    operation_time = mr.get('updated_at', '')
    operation_time = format_operation_time(operation_time)
    
    # 获取 MR 创建者信息
    mr_author_id = mr.get('author_id')
    mr_creator = None
    if mr_author_id:
        # 通过 GitLab API 获取作者信息
        gitlab_user = get_gitlab_user_info(mr_author_id)
        if gitlab_user:
            mr_creator = gitlab_user.get('name')
    
    # 获取所有参与者信息
    project_id = mr.get('source_project_id')
    mr_iid = mr.get('iid')
    participants = []
    if project_id and mr_iid:
        participants = get_mr_participants(project_id, mr_iid)
    
    # 构建参与者列表
    participant_names = []
    for p in participants:
        name = p.get('name')
        if name and name not in participant_names:
            participant_names.append(name)
    
    mr_info = {
        'title': mr.get('title', ''),
        'state': mr.get('state', ''),
        'action': mr.get('action', ''),
        'author': mr_creator or 'Unknown',  # 使用 MR 创建者信息
        'operator': operator.get('name', ''),  # 使用操作人信息
        'operation_time': operation_time,
        'url': mr.get('url', ''),
        'description': mr.get('description', ''),
        'participants': ', '.join(participant_names) if participant_names else 'None'  # 添加参与者信息
    }

    # 获取操作人信息
    operator_name = operator.get('name', '')
    operator_username = operator.get('username', '')
    operator_email = operator.get('email', '')
    
    # 尝试获取所有参与者的飞书用户信息并发送通知
    for participant in participants:
        username = participant.get('username')
        if username:
            user = get_user_by_username(username)
            if user and user.user_id:
                send_mr_notification(user.user_id, mr_info)

def handle_mr_comment(data: Dict[str, Any]) -> None:
    """处理 MR 评论事件"""
    # 检查是否是 MR 的评论
    if data.get('object_attributes', {}).get('noteable_type') != 'MergeRequest':
        return

    note = data.get('object_attributes', {})
    operator = data.get('user', {})  # 操作人信息
    mr = data.get('merge_request', {})
    
    # 获取操作时间
    operation_time = note.get('created_at', '')
    operation_time = format_operation_time(operation_time)
    
    # 获取 MR 创建者信息
    mr_author_id = mr.get('author_id')
    mr_creator = None
    if mr_author_id:
        # 通过 GitLab API 获取作者信息
        gitlab_user = get_gitlab_user_info(mr_author_id)
        if gitlab_user:
            mr_creator = gitlab_user.get('name')
    
    # 获取所有参与者信息
    project_id = mr.get('source_project_id')
    mr_iid = mr.get('iid')
    participants = []
    if project_id and mr_iid:
        participants = get_mr_participants(project_id, mr_iid)
    
    # 构建参与者列表
    participant_names = []
    for p in participants:
        name = p.get('name')
        if name and name not in participant_names:
            participant_names.append(name)
    
    mr_info = {
        'title': mr.get('title', ''),
        'state': mr.get('state', ''),
        'action': 'comment',
        'author': mr_creator or 'Unknown',  # 使用 MR 创建者信息
        'operator': operator.get('name', ''),  # 使用操作人信息
        'operation_time': operation_time,
        'url': note.get('url', ''),
        'description': note.get('note', ''),
        'participants': ', '.join(participant_names) if participant_names else 'None'  # 添加参与者信息
    }

    # 获取操作人信息
    operator_name = operator.get('name', '')
    operator_username = operator.get('username', '')
    operator_email = operator.get('email', '')
    
    # 尝试获取所有参与者的飞书用户信息并发送通知
    for participant in participants:
        username = participant.get('username')
        if username:
            user = get_user_by_username(username)
            if user and user.user_id:
                send_mr_notification(user.user_id, mr_info)

@app.route('/webhook/gitlab', methods=['POST'])
def gitlab_webhook():
    """处理 GitLab Webhook 请求"""
    # 验证签名
    # signature = request.headers.get('X-Gitlab-Token')
    # if not verify_gitlab_signature(request.get_data(), signature):
    #     return jsonify({'error': 'Invalid signature'}), 401

    try:
        data = request.get_json()
        print("收到回调，内容：" + str(data))
        print("---------------------------------------------------\n")
        handle_mr_event(data)
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        print(e)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=4500)
