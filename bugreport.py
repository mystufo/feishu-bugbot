import json
import lark_oapi as lark
import os
import time
import tempfile
import requests
from analyse import BugAnalyzer
from collections import OrderedDict
from dotenv import load_dotenv
from lark_oapi.api.im.v1 import *
from lark_oapi.api.contact.v3 import *
from lark_oapi.api.drive.v1 import *
from zentao.zentao import ZenTaoAPI
from pypinyin import lazy_pinyin
import re
# 加载环境变量
load_dotenv()

# 从环境变量获取配置
ZENTAO_BASE_URL = os.getenv('ZENTAO_BASE_URL', '').rstrip('/')
ZENTAO_USERNAME = os.getenv('ZENTAO_USERNAME')
ZENTAO_PASSWORD = os.getenv('ZENTAO_PASSWORD')
FEISHU_APP_ID = os.getenv('FEISHU_APP_ID')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET')
# 机器人回复中提示用户联系的负责人，可按需配置
BUG_CONTACT_NAME = os.getenv('BUG_CONTACT_NAME', '管理员')
# 群名不在 group_config.json 中（或私聊机器人）时，bug 默认创建到这个禅道产品下
try:
    ZENTAO_DEFAULT_PRODUCT_ID = int(os.getenv('ZENTAO_DEFAULT_PRODUCT_ID', '1'))
except ValueError:
    raise SystemExit("环境变量 ZENTAO_DEFAULT_PRODUCT_ID 必须是整数")

_missing = [name for name, value in {
    'ZENTAO_BASE_URL': ZENTAO_BASE_URL,
    'ZENTAO_USERNAME': ZENTAO_USERNAME,
    'ZENTAO_PASSWORD': ZENTAO_PASSWORD,
    'FEISHU_APP_ID': FEISHU_APP_ID,
    'FEISHU_APP_SECRET': FEISHU_APP_SECRET,
}.items() if not value]
if _missing:
    raise SystemExit(f"缺少必要的环境变量: {', '.join(_missing)}，请参考 .env.example 配置 .env 文件")

# 初始化Bug分析器
bug_analyzer = BugAnalyzer()

# 用于存储已处理的消息ID，防止重复处理
processed_messages = OrderedDict()
MAX_PROCESSED_MESSAGES = 1000  # 最多保存1000条消息记录

# 加载群聊配置
def load_group_config(path=None):
    if path is None:
        # 获取当前脚本（bugreport.py）所在目录
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_config.json")
    if not os.path.exists(path):
        print("没有找到配置文件：", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

GROUP_CONFIG = load_group_config()

def download_file(file_key: str, file_name: str, message_id: str, file_type: str) -> str:
    """
    下载飞书文件到临时目录
    
    Args:
        file_key: 文件key
        file_name: 文件名
        message_id: 消息ID
        file_type: 文件类型 (image 或 file)
        
    Returns:
        str: 下载后的文件路径
    """
    # 获取文件下载地址
    request = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(file_key) \
        .type(file_type) \
        .build()
    
    response = client.im.v1.message_resource.get(request)
    if not response.success():
        raise Exception(f"获取文件下载地址失败: {response.msg}, code: {response.code}")
    
    # 创建临时文件
    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, file_name)
    
    # 保存文件
    with open(file_path, 'wb') as f:
        f.write(response.file.read())
    
    return file_path

def handle_attachment(message_type: str, content: dict, message_id: str) -> tuple[list[str], str]:
    """
    处理附件消息，返回所有文件路径和描述信息

    Args:
        message_type: 消息类型
        content: 消息内容
        message_id: 消息ID

    Returns:
        tuple: (文件路径列表, 描述信息)
    """
    # 处理post类型的消息
    if message_type == "post":
        post_content = content["content"]  # 直接获取content数组
        file_paths = []
        description = None

        # 遍历post内容，查找所有图片或视频
        for elements in post_content:
            for element in elements:
                if element.get("tag") == "img":
                    file_key = element.get("image_key")
                    file_name = f"image_{int(time.time())}_{len(file_paths)}.png"
                    description = "收到图片附件"
                    file_type = "image"
                    file_path = download_file(file_key, file_name, message_id, file_type)
                    file_paths.append(file_path)
                elif element.get("tag") == "media":
                    file_key = element.get("file_key")
                    file_name = f"video_{int(time.time())}_{len(file_paths)}.mp4"
                    description = "收到视频附件"
                    file_type = "file"
                    file_path = download_file(file_key, file_name, message_id, file_type)
                    file_paths.append(file_path)

        if not file_paths:
            raise ValueError("未找到图片或视频附件")
        return file_paths, description
    else:
        raise ValueError(f"不支持的消息类型: {message_type}")

def extract_first_at_user_from_text_msg(message, client):
    """
    从 message.mentions 里提取第一个被@的、且在员工库中的用户 open_id 和 name
    """
    mentions = getattr(message, "mentions", None)
    if mentions and len(mentions) > 0:
        for mention in mentions:
            open_id = getattr(getattr(mention, "id", None), "open_id", None)
            name = getattr(mention, "name", None)
            if open_id and is_employee(open_id, client):
                return open_id, name
    return None, None

def is_message_processed(message_id: str) -> bool:
    """
    检查消息是否已经处理过
    
    Args:
        message_id: 消息ID
        
    Returns:
        bool: 是否已处理
    """
    # 清理过期的消息记录
    current_time = time.time()
    while processed_messages and current_time - processed_messages[next(iter(processed_messages))] > 86400:  # 12小时过期
        processed_messages.popitem(last=False)
    
    # 如果消息已处理，返回True
    if message_id in processed_messages:
        return True
    
    # 记录新消息
    processed_messages[message_id] = current_time
    
    # 如果记录数超过限制，删除最旧的记录
    if len(processed_messages) > MAX_PROCESSED_MESSAGES:
        processed_messages.popitem(last=False)
    
    return False


def send_message(data: P2ImMessageReceiveV1, content: str) -> None:
    """
    发送消息给用户
    
    Args:
        data: 消息事件数据
        content: 要发送的消息内容
    """
    message_content = json.dumps(
        {
            "text": content
        }
    )

    #私聊消息回复
    if data.event.message.chat_type == "p2p":
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(data.event.message.chat_id)
                .msg_type("text")
                .content(message_content)
                .build()
            )
            .build()
        )
        response = client.im.v1.chat.create(request)

        if not response.success():
            raise Exception(
                f"client.im.v1.chat.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}"
            )
    #群聊消息回复
    else:
        request: ReplyMessageRequest = (
            ReplyMessageRequest.builder()
            .message_id(data.event.message.message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(message_content)
                .msg_type("text")
                .build()
            )
            .build()
        )
        response: ReplyMessageResponse = client.im.v1.message.reply(request)
        if not response.success():
            raise Exception(
                f"client.im.v1.message.reply failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}"
            )

# 获取群组信息，用于确定用例标题的标签和product id
def get_group_name_by_chat_id(chat_id, client):
    request = GetChatRequest.builder().chat_id(chat_id).build()
    response = client.im.v1.chat.get(request)
    if response.success() and response.data and hasattr(response.data, 'name'):
        return response.data.name
    return None

def chinese_name_to_pinyin(name):
    return ''.join(lazy_pinyin(name)).lower()

def get_feishu_user_name_by_openid(open_id, client):
    from lark_oapi.api.contact.v3 import GetUserRequest
    request = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
    response = client.contact.v3.user.get(request)
    if response.success() and response.data and response.data.user:
        return response.data.user.name  # 真实中文姓名
    return None

def is_employee(open_id, client):
    """
    通过飞书API判断 open_id 是否为公司员工
    """
    try:
        request = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
        response = client.contact.v3.user.get(request)
        if response.success() and response.data and response.data.user:
            return True
    except Exception as e:
        print(f"员工校验异常: {e}")
    return False

# 注册接收消息事件，处理接收到的消息。
def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    # 检查消息是否已处理
    message_id = data.event.message.message_id
    print(f">>>>>>>>>>>>>>>>>>>>接收到消息，消息id：{message_id}<<<<<<<<<<<<<<<<<<<<")
    if is_message_processed(message_id):
        print(f"消息 {message_id} 已处理，跳过")
        return
    
    # 获取群名称、前缀、project_id
    group_name = None
    group_prefix = ""
    project_id = ZENTAO_DEFAULT_PRODUCT_ID
    if data.event.message.chat_type == "group":
        chat_id = data.event.message.chat_id
        print("chat_id:", chat_id)
        group_name = get_group_name_by_chat_id(chat_id, client)
        print("群名称：", group_name)
        group_cfg = GROUP_CONFIG.get(group_name)
        if group_cfg:
            group_prefix = group_cfg.get("prefix", "")
            project_id = group_cfg.get("project_id", ZENTAO_DEFAULT_PRODUCT_ID)

    # 获取发送者ID
    open_id = data.event.sender.sender_id.open_id
    
    # 获取提bug用户的相关信息
    try:
        # 使用OpenAPI获取用户信息
        request = GetUserRequest.builder().user_id(open_id).build()
        user_response = client.contact.v3.user.get(request)
        
        if user_response.success():
            user_info = user_response.data.user
            user_email = user_info.email
            print(f"发送者姓名: {user_info.name}")
            print(f"发送者邮箱: {user_info.email}")
            print(f"用户ID: {user_info.user_id}")
            print(f"Open ID: {user_info.open_id}")
        else:
            print(f"获取用户信息失败: {user_response.msg}")
            print(f"错误代码: {user_response.code}")
            print(f"请求ID: {user_response.get_log_id()}")
    except Exception as e:
        print(f"获取用户信息时发生错误: {str(e)}")
        print(f"错误类型: {type(e)}")

    res_content = ""
    downloaded_files = []
    
    # 对用户提的bug以消息的方式进行回应
    try:
        has_at_all = False
        zentao_username = None
        message_type = data.event.message.message_type
        content = json.loads(data.event.message.content)
        print("content:" + str(content))
        
        #纯文本消息
        if message_type == "text":
            res_content = content["text"]
            # 去除所有 @_user_x 片段（包括前后空格）
            res_content = re.sub(r'@_user_\w+\s*', '', res_content)
            res_content = res_content.strip()
            # 优先用 mentions 提取@人
            at_user_id, at_user_name = extract_first_at_user_from_text_msg(data.event.message, client)
            if at_user_id:
                zentao_username = chinese_name_to_pinyin(at_user_name)
                # if at_real_name:
                    # zentao_username = chinese_name_to_pinyin(at_real_name)
        #文字+图片和文字+视频的消息
        elif message_type == "post":
            # 提取post中的文本内容
            post_content = content["content"]
            text_content = ""
            # 检查是否@所有人
            for elements in post_content:
                for element in elements:
                    if element.get("tag") == "at" and element.get("user_id") == "@_all":
                        has_at_all = True
                    elif element.get("tag") == "text":
                        text_content += element.get("text", "") + "\n"
            # 提取第一个@的人
            at_user_id, at_user_name = extract_first_at_user_from_text_msg(data.event.message, client)
            if at_user_id:
                zentao_username = chinese_name_to_pinyin(at_user_name)
                # at_real_name = get_feishu_user_name_by_openid(at_user_id, client)
                # if at_real_name:
                #     zentao_username = chinese_name_to_pinyin(at_real_name)

            # 处理附件
            try:
                message_id = data.event.message.message_id  # 获取消息ID
                file_paths, description = handle_attachment(message_type, content, message_id)
                downloaded_files.extend(file_paths)  # 支持多附件
                res_content = text_content
            except ValueError as e:
                # 如果没有附件，只使用文本内容
                res_content = text_content
        #只发送文件的消息
        else:
            send_message(data, "请把bug描述和对应的图片/视频放在一个消息里发送")
            return

        # 首先检查是否为bug描述
        try:
            is_bug, reason = bug_analyzer.check_is_bug(res_content)
            if not is_bug:
                print("text:" + res_content)
                if "@_all" in res_content or has_at_all:
                    print("解析失败并@了全部人，不发送消息")
                    return
                send_message(data, f"您输入的信息没有成功解析成bug描述，请尝试重新输入，并参考以下规则：\n\n1. 描述您遇到的具体问题\n2. 提供详细的操作步骤\n3. 说明预期结果和实际结果\n\n如果多次输入失败，请联系{BUG_CONTACT_NAME}解决")
                return
            else:
                # 分析用户输入并创建bug
                bug_info = bug_analyzer.analyze(res_content)
                
                # 调用禅道提bug
                zentao = ZenTaoAPI(
                    base_url=ZENTAO_BASE_URL,
                    username=ZENTAO_USERNAME,
                    password=ZENTAO_PASSWORD
                )
                
                # 添加用户信息到bug描述中
                steps = f"bug描述：\n{res_content}\n报告人：{user_info.name}"

                # 拼接标题前缀
                bug_title = f"{group_prefix}{bug_info['title']}"
                
                # 创建bug，包含附件
                bug_kwargs = dict(
                    product_id=project_id,
                    title=bug_title,
                    steps=steps,
                    type=bug_info["type"],
                    severity=int(bug_info["severity"]),
                    pri=int(bug_info["pri"]),
                    files=downloaded_files if downloaded_files else None,
                    mailto=[chinese_name_to_pinyin(user_info.name)]
                )
                if zentao_username:
                    bug_kwargs['assignedTo'] = zentao_username
                print("创建bug的相关参数:")
                print(bug_kwargs)
                result = zentao.create_bug(**bug_kwargs)
                
                # 检查创建结果
                if result and isinstance(result, dict) and 'id' in result and result['id']:
                    # 创建成功
                    send_message(data, f"已成功创建Bug，标题：{bug_info['title']}\n\n禅道链接：{ZENTAO_BASE_URL}/index.php?m=bug&f=view&bugID={result['id']}")
                else:
                    # 如果是@所有人，则不创建bug
                    if "@_all" in res_content:
                        return
                    # 创建失败
                    error_msg = result.get('message', '未知错误') if result else '创建失败'
                    send_message(data, f"Bug创建失败：{error_msg}\n请稍后重试或联系{BUG_CONTACT_NAME}解决")
            
        except Exception as e:
            send_message(data, f"处理失败：{str(e)}")
        finally:
            # 清理下载的临时文件
            for file_path in downloaded_files:
                try:
                    os.remove(file_path)
                except Exception as e:
                    print(f"清理临时文件失败: {str(e)}")

    except Exception as e:
        send_message(data, f"处理消息失败：{str(e)}")

    

# 注册事件回调
event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
    .build()
)

# 创建 LarkClient 对象，用于请求OpenAPI, 并创建 LarkWSClient 对象，用于使用长连接接收事件。
client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
wsClient = lark.ws.Client(
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    event_handler=event_handler,
    log_level=lark.LogLevel.DEBUG,
)

def main():
    #  启动长连接，并注册事件处理器。
    wsClient.start()

if __name__ == "__main__":
    main()
