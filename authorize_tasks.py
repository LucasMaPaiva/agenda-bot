import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

TASKS_SCOPES = ["https://www.googleapis.com/auth/tasks"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autoriza uma conta pessoal para o Google Tarefas."
    )
    parser.add_argument("role", choices=["matheus", "leticia"])
    parser.add_argument("client_secret", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    args = parser.parse_args()

    flow = InstalledAppFlow.from_client_secrets_file(
        args.client_secret, scopes=TASKS_SCOPES
    )
    credentials = flow.run_local_server(port=0, prompt="consent")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"tasks-{args.role}.json"
    output.write_text(credentials.to_json(), encoding="utf-8")
    output.chmod(0o600)
    print(f"Autorização salva em {output}")


if __name__ == "__main__":
    main()
