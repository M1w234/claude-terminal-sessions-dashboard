#!/usr/bin/env python3
"""
Separate terminal WebSocket server.
Runs on port 3457 to avoid blocking the main dashboard API.
"""

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3456", "http://127.0.0.1:3456"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _set_pty_size(fd: int, rows: int, cols: int):
    size = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


@app.websocket("/ws/terminal")
async def terminal_websocket(websocket: WebSocket):
    await websocket.accept()

    child_pid, fd = pty.fork()

    if child_pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLORTERM"] = "truecolor"
        os.execvp("/bin/zsh", ["/bin/zsh", "-l"])

    _set_pty_size(fd, 24, 80)

    # Non-blocking fd
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    loop = asyncio.get_event_loop()

    async def read_pty():
        try:
            read_event = asyncio.Event()

            def on_readable():
                read_event.set()

            loop.add_reader(fd, on_readable)

            while True:
                await read_event.wait()
                read_event.clear()
                try:
                    while True:
                        data = os.read(fd, 4096)
                        if data:
                            await websocket.send_bytes(data)
                        else:
                            return
                except BlockingIOError:
                    pass
                except OSError:
                    return
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass

    read_task = asyncio.create_task(read_pty())

    try:
        while True:
            msg = await websocket.receive()

            if msg.get("type") == "websocket.disconnect":
                break

            if "text" in msg:
                try:
                    ctrl = json.loads(msg["text"])
                    if ctrl.get("type") == "resize":
                        _set_pty_size(fd, ctrl.get("rows", 24), ctrl.get("cols", 80))
                        continue
                except (json.JSONDecodeError, KeyError):
                    os.write(fd, msg["text"].encode())
                    continue

            if "bytes" in msg:
                os.write(fd, msg["bytes"])

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        read_task.cancel()
        try:
            os.kill(child_pid, signal.SIGTERM)
            os.waitpid(child_pid, 0)
        except (OSError, ChildProcessError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
