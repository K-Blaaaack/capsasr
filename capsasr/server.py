import json
import os
import socket
import threading


class Server:
    """Unix socket 接口：接收 {"wav": path}，返回 {"ok", "text"}。

    供 CLI/脚本转写任意音频文件，不经数据集落盘。
    """

    def __init__(self, socket_path: str, transcribe_fn):
        self.path = socket_path
        self.transcribe = transcribe_fn
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        os.chmod(self.path, 0o600)
        self._srv.listen(4)
        self._thread.start()
        print(f"[server] {self.path}", flush=True)

    def stop(self):
        try:
            self._srv.close()
        finally:
            if os.path.exists(self.path):
                os.unlink(self.path)

    def _loop(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            fp = conn.makefile("r")
            req = json.loads(fp.readline())
            text = self.transcribe(req["wav"])
            reply = {"ok": True, "text": text}
        except Exception as e:
            reply = {"ok": False, "error": str(e)}
        try:
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())
        except (BrokenPipeError, OSError):
            pass
        finally:
            conn.close()
