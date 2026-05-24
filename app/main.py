from __future__ import annotations

from .chat_logic import ChatBot


def main() -> None:
    bot = ChatBot()
    display_name = bot.persona.display_name

    print("=== AI 多人格聊天系统（命令行版）===")
    print("输入内容开始聊天，输入 /exit、退出 或 q 结束。\n")

    while True:
        user_input = input("你：").strip()
        if user_input.lower() in {"/exit", "退出", "q"}:
            print(f"{display_name}：那我先在这里。下次见。")
            break

        if not user_input:
            continue

        try:
            reply = bot.chat(user_input)
            print(f"{display_name}：{reply}\n")
        except Exception as exc:
            print(f"{display_name}：刚才出了一点问题。")
            print("错误信息：", exc)


if __name__ == "__main__":
    main()
