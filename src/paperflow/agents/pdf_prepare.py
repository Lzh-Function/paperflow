"""隔離済みC:作業コピーを整える内部ワーカー。LLM・Vaultへは接続しない。"""

import json
import sys
from pathlib import Path

from paperflow.agents.base import ProviderError
from paperflow.agents.document import claude_pdf_parts


def main():
    pdf, cwd = [Path(value).resolve() for value in sys.argv[1:]]
    if pdf.parent != cwd or not pdf.is_file():
        raise ValueError("内部PDFワーカーには同じ隔離フォルダの入力を指定してください")
    try:
        parts, method = claude_pdf_parts(pdf, cwd)
        result = dict(
            method=method,
            parts=[
                dict(file=p.path.name, first_page=p.first_page, last_page=p.last_page)
                for p in parts
            ],
        )
    except ProviderError as exc:
        result = dict(error=exc.category, message=str(exc))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
