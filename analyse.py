import json
import os
import requests
from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from typing import Dict, Any, Tuple

from utils.ark_openai_env import get_ark_openai_config


class BugAnalyzer:
    def __init__(self):
        """
        初始化Bug分析器
        
        """
        load_dotenv()
        self.api_key, self.api_url, self.model_name = get_ark_openai_config()
        self.software_context = self._build_software_context()

        # 定义判断是否为bug描述的提示模板
        self.check_template = PromptTemplate(
            input_variables=["software_context", "user_input"],
            template="""
            请判断用户的输入是否是一个bug描述。判断标准：
            1. 是否在描述系统或程序的问题、异常或不符合预期的情况
            2. 是否提到了功能或界面的不一致、缺失或错误
            3. 是否表达了对当前状态的疑问或不满
            
            软件背景：
            {software_context}
            
            注意：
            - 用户可能是产品、UI等非专业测试人员，描述可能不够专业或完整
            - 即使描述不完整，只要是在描述问题或异常，也应该判定为bug
            - 如果用户提到"问题"、"异常"、"错误"、"bug"、"不对"、"不应该"、"没有"等关键词，很可能是bug描述
            - 如果用户是在询问功能或提出新需求，则不是bug
            - 如果用户是在描述当前状态与预期不符，应该判定为bug
            - 如果用户是在询问如何使用某个功能，应该判定为bug（因为可能是功能不清晰或缺少引导）
            
            请用JSON格式回答：
            {{
                "is_bug": true/false,
                "reason": "判断理由"
            }}
            
            用户输入：{user_input}
            """
        )
        
        # 定义bug分析提示模板
        self.prompt_template = PromptTemplate(
            input_variables=["software_context", "user_input"],
            template="""
            请尝试把用户的自然语言描述转换为结构化的bug报告格式。
            
            软件背景：
            {software_context}
            
            请仔细分析用户描述的问题，提取关键信息，并按照以下JSON格式输出，不要包含任何其他文字：
            {{
                "title": "简短的bug标题，不超过20个字",
                "steps": "详细的复现步骤，必须包含：\n1. 操作步骤（具体到每个点击和输入）\n2. 预期结果（系统应该表现的行为）\n3. 实际结果（系统实际表现的行为）",
                "type": "bug类型，必须是以下值之一：codeerror(代码错误)、interface(界面优化)、config(配置相关)、install(安装部署)、security(安全相关)、performance(性能问题)、standard(标准规范)、automation(测试脚本)、design(设计相关)、others(其他)",
                "severity": "严重程度，必须是1-4的整数，1最严重",
                "pri": "优先级，必须是1-4的整数，1最高"
            }}

            用户输入：{user_input}
            
            请确保：
            1. 输出是有效的JSON格式
            2. 不要包含任何其他文字说明
            3. 所有字段都必须存在且格式正确
            4. severity和pri必须是整数
            5. 直接输出JSON，不要有任何前缀或后缀
            6. 如果用户描述不完整，不用额外补充缺失的信息
            7. 如果用户没有明确说明操作步骤，不用额外补充可能的操作步骤
            8. 如果用户没有明确说明预期结果，不用额外补充预期结果
            9. 如果用户没有明确说明实际结果，不用额外补充实际结果
            10. 如果用户是在询问如何使用某个功能，请将其视为功能不清晰或缺少引导的问题
            11. 如果用户的描述不够全面（如只描述了问题但没有具体步骤），请使用以下格式：
                {{
                    "title": "根据用户描述总结的标题",
                    "steps": "用户原始描述",
                    "type": "根据问题类型判断",
                    "severity": 3,
                    "pri": 3
                }}
            12. 如果用户描述中包含了时间信息（如"周二"、"周四"等），请将其作为步骤的一部分保留
            13. 如果用户描述中包含了工期信息（如"1-2天"），请将其作为步骤的一部分保留
            """
        )

    @staticmethod
    def _build_software_context() -> str:
        """
        从环境变量组装提示词中的“软件背景”段落，便于不同产品复用。

        可配置项：PRODUCT_NAME、PRODUCT_PLATFORMS、PRODUCT_DESCRIPTION、PRODUCT_USERS。
        """
        items = [
            ("软件名称", os.getenv("PRODUCT_NAME", "待测产品")),
            ("平台", os.getenv("PRODUCT_PLATFORMS", "Web端和移动端")),
            ("主要功能", os.getenv("PRODUCT_DESCRIPTION", "请在 .env 中通过 PRODUCT_DESCRIPTION 描述产品的主要功能")),
            ("用户群体", os.getenv("PRODUCT_USERS", "包括产品、UI等非专业测试人员")),
        ]
        return "\n            ".join(f"- {label}：{value}" for label, value in items)

    def _chat_completions_url(self) -> str:
        """
        解析 OpenAI 兼容接口的完整 URL。

        方舟等平台配置的 Base URL 通常为 .../api/v3，需拼接 /chat/completions；
        若环境变量已包含该路径则不再重复追加。
        """
        if not self.api_url:
            raise ValueError("环境变量 ARK_BASE_URL（或 DEEPSEEK_URL）未设置")
        base = self.api_url.strip().rstrip("/")
        suffix = "/chat/completions"
        if base.endswith(suffix):
            return base
        return f"{base}{suffix}"

    def _call_llm_api(self, prompt: str) -> str:
        """
        调用方舟 OpenAI 兼容 Chat Completions 接口。

        Args:
            prompt: 提示文本

        Returns:
            str: API 响应文本
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        data = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "你是一个专业的测试工程师，负责分析用户输入并输出JSON格式的结果。"},
                {"role": "user", "content": prompt}
            ]
        }
        
        try:
            response = requests.post(
                self._chat_completions_url(), headers=headers, json=data
            )
            response.raise_for_status()
            
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            print("原始输出:", content)  # 打印原始输出以便调试
            return content
        except requests.exceptions.RequestException as e:
            print(f"API调用失败: {str(e)}")
            print(f"响应状态码: {response.status_code}")
            print(f"响应内容: {response.text}")
            raise Exception(f"API调用失败: {str(e)}")

    def check_is_bug(self, user_input: str) -> Tuple[bool, str]:
        """
        检查用户输入是否为bug描述
        
        Args:
            user_input: 用户的输入文本
            
        Returns:
            Tuple[bool, str]: (是否为bug描述, 判断理由)
        """
        prompt = self.check_template.format(software_context=self.software_context, user_input=user_input)
        result = self._call_llm_api(prompt)
        
        try:
            # 清理输出
            result = result.strip()
            if result.startswith("```json"):
                result = result[7:]
            if result.endswith("```"):
                result = result[:-3]
            result = result.strip()
            
            # 解析JSON
            check_result = json.loads(result)
            return check_result["is_bug"], check_result["reason"]
        except Exception as e:
            print(f"解析判断结果失败: {str(e)}")
            return False, "无法判断输入内容是否为bug描述"

    def analyze(self, user_input: str) -> Dict[str, Any]:
        """
        分析用户输入并转换为bug格式
        
        Args:
            user_input: 用户的自然语言描述
            
        Returns:
            Dict: 结构化的bug信息
        """
        # 准备提示文本
        prompt = self.prompt_template.format(software_context=self.software_context, user_input=user_input)
        
        # 调用API
        result = self._call_llm_api(prompt)
        
        try:
            # 尝试清理输出中的非JSON内容
            result = result.strip()
            if result.startswith("```json"):
                result = result[7:]
            if result.endswith("```"):
                result = result[:-3]
            result = result.strip()
            
            # 解析JSON结果
            bug_info = json.loads(result)
            
            # 验证必要字段
            required_fields = ["title", "steps", "type", "severity", "pri"]
            for field in required_fields:
                if field not in bug_info:
                    raise Exception(f"缺少必要字段: {field}")
            
            # 验证数值字段
            if not isinstance(bug_info["severity"], int) or not 1 <= bug_info["severity"] <= 4:
                raise Exception("severity必须是1-4的整数")
            if not isinstance(bug_info["pri"], int) or not 1 <= bug_info["pri"] <= 4:
                raise Exception("pri必须是1-4的整数")
            
            return bug_info
        except json.JSONDecodeError as e:
            print(f"JSON解析错误: {str(e)}")
            print(f"尝试解析的内容: {result}")
            raise Exception(f"解析LLM输出失败: {str(e)}")
        except Exception as e:
            raise Exception(f"验证bug信息失败: {str(e)}")

def main():
    # 使用示例
    analyzer = BugAnalyzer()
    
    # 测试用例
    user_input = """
    这个hoover对比非常不明显，昨天下午就提过了，我说的不明显是指你至少加一条可视的线
    """
    
    try:
        # 首先检查是否为bug描述
        is_bug, reason = analyzer.check_is_bug(user_input)
        if not is_bug:
            print(f"这不是一个bug描述: {reason}")
            return
            
        # 如果是bug描述，则进行分析
        bug_info = analyzer.analyze(user_input)
        print("分析结果:", json.dumps(bug_info, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"分析失败: {str(e)}")

if __name__ == "__main__":
    main() 