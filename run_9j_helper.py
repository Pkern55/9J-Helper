"""Entry point for PyInstaller and `py run_9j_helper.py`.

Hidden check for builds:  9J-Helper.exe --selftest <9J.pdf> <out.txt>
"""

import sys


def _selftest(pdf: str, out: str) -> int:
    try:
        import gspread  # noqa: F401  (make sure the Google libs are bundled)
        from nine_j_helper.nine_j import parse_pdf
        doc = parse_pdf(pdf)
        msg = f"OK release={doc.release_id} pos={','.join(doc.pos)} lines={len(doc.lines)}\n"
        code = 0
    except Exception as e:  # noqa: BLE001
        msg, code = f"FAIL {type(e).__name__}: {e}\n", 1
    with open(out, "w", encoding="utf-8") as f:
        f.write(msg)
    return code


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--selftest":
        sys.exit(_selftest(sys.argv[2], sys.argv[3]))
    from nine_j_helper.app import main
    main()
