import json
import os
from typing import Any, Dict, List, Optional

import requests


class ZenTaoAPI:
    def __init__(self, base_url: str, username: str, password: str):
        """
        初始化禅道API客户端

        Args:
            base_url: 禅道服务器地址，例如：http://zentao.example.com
            username: 禅道用户名
            password: 禅道密码
        """
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.token = None
        self._login()

    def _login(self) -> None:
        """登录禅道系统"""
        login_url = f"{self.base_url}/api.php/v1/tokens"
        data = {
            "account": self.username,
            "password": self.password
        }

        response = self.session.post(login_url, json=data)
        response.raise_for_status()

        result = response.json()
        if 'token' in result:
            self.token = result['token']
            print("禅道登录成功")
            # 设置后续请求的认证头
            self.session.headers.update({
                'Authorization': f'Bearer {self.token}'
            })
        else:
            raise Exception("登录失败: 未获取到token")

    def create_bug(self,
                   product_id: int,
                   title: str,
                   steps: str,
                   type: str = 'codeerror',
                   severity: int = 3,
                   pri: int = 3,
                   openedBuild: str = '主干',
                   assignedTo: Optional[str] = None,
                   files: Optional[List[str]] = None,
                   **kwargs) -> Dict[str, Any]:
        """
        创建bug

        Args:
            product_id: 产品ID
            title: bug标题
            steps: 重现步骤
            type: bug类型，默认为'codeerror'
            severity: 严重程度(1-4)，默认为3
            pri: 优先级(1-4)，默认为3
            openedBuild: 打开版本，默认为'主干'
            assignedTo: 指派给（禅道账号），为空时不指派
            files: 附件文件路径列表，例如 ['/path/to/screenshot.png']
            **kwargs: 其他可选参数

        Returns:
            Dict: 创建结果
        """
        if not self.token:
            raise Exception("未登录")

        create_url = f"{self.base_url}/api.php/v1/products/{product_id}/bugs"

        # 准备基本数据
        data = {
            "title": title,
            "steps": steps,
            "type": type,
            "severity": severity,
            "pri": pri,
            "openedBuild": openedBuild,
            **kwargs
        }
        if assignedTo:
            data["assignedTo"] = assignedTo

        # 准备文件数据
        files_data = {}
        if files:
            for i, file_path in enumerate(files):
                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"文件不存在: {file_path}")
                file_name = os.path.basename(file_path)
                files_data[f'files[{i}]'] = (file_name, open(file_path, 'rb'))

        try:
            # 发送请求
            response = self.session.post(
                create_url,
                data=data,
                files=files_data,
                headers={'Token': self.token}
            )
            print(f"创建bug响应: {response.text}")
            response.raise_for_status()
            return response.json()
        finally:
            # 确保所有文件都被关闭
            for file_tuple in files_data.values():
                if isinstance(file_tuple, tuple) and len(file_tuple) > 1:
                    file_tuple[1].close()


def main():
    """使用示例：从环境变量读取禅道配置并创建一个测试 bug。"""
    from dotenv import load_dotenv

    load_dotenv()
    base_url = os.getenv("ZENTAO_BASE_URL")
    username = os.getenv("ZENTAO_USERNAME")
    password = os.getenv("ZENTAO_PASSWORD")
    if not all([base_url, username, password]):
        raise SystemExit("请先配置 ZENTAO_BASE_URL、ZENTAO_USERNAME、ZENTAO_PASSWORD")

    zentao = ZenTaoAPI(base_url=base_url, username=username, password=password)

    # 创建bug示例（如需附件，传入 files=['/path/to/file']）
    try:
        result = zentao.create_bug(
            product_id=int(os.getenv("ZENTAO_DEFAULT_PRODUCT_ID", "1")),
            title="测试Bug",
            steps="1. 打开页面\n2. 点击按钮\n3. 出现错误",
            severity=3,
            pri=3,
        )
        print("Bug创建结果:", json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"创建Bug失败: {str(e)}")


if __name__ == "__main__":
    main()
