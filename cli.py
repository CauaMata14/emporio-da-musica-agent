"""Terminal chat with the Empório da Música agent.

    python cli.py                    # new conversation
    python cli.py --session joao     # resume (or start) a named conversation
    python cli.py --debug            # show the tools the agent called
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from emporio_agent.agent import EmporioAgent  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat com a Lia, assistente da Empório da Música")
    parser.add_argument("--session", default=None, help="ID da conversa (retoma o histórico salvo)")
    parser.add_argument("--debug", action="store_true", help="mostra as chamadas de ferramentas")
    args = parser.parse_args()

    session = args.session or f"cli-{uuid.uuid4().hex[:8]}"
    agent = EmporioAgent()

    print(f"🎸 Empório da Música — conversa '{session}'. Digite 'sair' para encerrar.\n")
    for role, text in agent.history_for_display(session):
        print(f"{'Você' if role == 'user' else 'Lia'}: {text}\n")

    while True:
        try:
            user = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in {"sair", "exit", "quit"}:
            break
        reply = agent.chat(session, user)
        if args.debug:
            for call in reply.tool_calls:
                print(f"  [tool] {call.name}({json.dumps(call.input, ensure_ascii=False)})"
                      f"{' -> ERRO' if call.is_error else ''}")
        print(f"\nLia: {reply.text}\n")
    print(f"Até mais! Para retomar: python cli.py --session {session}")


if __name__ == "__main__":
    main()
