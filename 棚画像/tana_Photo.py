import asyncio
import struct
import threading
import time
import tkinter as tk
from bleak import BleakClient, BleakScanner
import cv2
import numpy as np
from PIL import Image, ImageTk

DEVICE_NAME = "ESP32S3-CAM-BLE"
TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
MAX_IMG_BYTES = 500_000

WINDOW_WIDTH = 900
WINDOW_HEIGHT = 700
HEADER_HEIGHT = 56
FOOTER_HEIGHT = 44
CAMERA_VIEW_WIDTH = 720
CAMERA_VIEW_HEIGHT = 540


class FrameAssembler:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.expected_len: int | None = None
        self.latest_img: np.ndarray | None = None
        self.updated = False
        self.last_recv_str = "--:--:--"

    def reset(self) -> None:
        self.buf.clear()
        self.expected_len = None

    def _resync_from_marker(self, start_from: int = 0) -> None:
        next_idx = self.buf.find(b"IMG:", start_from)
        if next_idx == -1:
            if len(self.buf) > 3:
                del self.buf[:-3]
            else:
                self.buf.clear()
        else:
            del self.buf[:next_idx]
        self.expected_len = None

    def feed(self, data: bytes) -> None:
        self.buf.extend(data)

        while True:
            if self.expected_len is None:
                idx = self.buf.find(b"IMG:")
                if idx == -1:
                    if len(self.buf) > 3:
                        del self.buf[:-3]
                    return
                if idx > 0:
                    del self.buf[:idx]

                if len(self.buf) < 8:
                    return

                (img_len,) = struct.unpack("<I", self.buf[4:8])
                if img_len <= 0 or img_len > MAX_IMG_BYTES:
                    self._resync_from_marker(start_from=4)
                    continue

                del self.buf[:8]
                self.expected_len = img_len

            if len(self.buf) < self.expected_len:
                return

            candidate = bytes(self.buf[: self.expected_len])
            del self.buf[: self.expected_len]
            self.expected_len = None

            if b"IMG:" in candidate:
                self._resync_from_marker(start_from=0)
                continue

            if not (candidate.startswith(b"\xff\xd8") and candidate.endswith(b"\xff\xd9")):
                continue

            arr = np.frombuffer(candidate, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None or img.size == 0:
                continue

            self.latest_img = img
            self.updated = True
            self.last_recv_str = time.strftime("%H:%M:%S")


class HumidityCabinetApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("防湿棚の状況")
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.configure(bg="#14181f")

        self.assembler = FrameAssembler()
        self.photo_image: ImageTk.PhotoImage | None = None

        self.status_text = "状態: 未接続"
        self.manual_disconnect = False
        self.worker_stop = threading.Event()
        self.disconnect_trigger = threading.Event()
        self.connect_trigger = threading.Event()

        self._build_ui()

        self.ble_thread = threading.Thread(target=self._ble_thread_main, daemon=True)
        self.ble_thread.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_ui()

    def _build_ui(self) -> None:
        # 上部ヘッダー
        header = tk.Frame(self.root, bg="#1d232d", height=HEADER_HEIGHT, padx=16, pady=6)
        header.pack(fill=tk.X, side=tk.TOP)
        header.pack_propagate(False)

        title_label = tk.Label(
            header,
            text="防湿棚の状況",
            font=("Meiryo UI", 18, "bold"),
            fg="#f3f6fa",
            bg="#1d232d",
        )
        title_label.pack(side=tk.LEFT, anchor="center")

        btn_frame = tk.Frame(header, bg="#1d232d")
        btn_frame.pack(side=tk.RIGHT, anchor="center")

        self.btn_connect = tk.Button(
            btn_frame,
            text="接続",
            font=("Meiryo UI", 11, "bold"),
            width=8,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_connect_clicked,
        )
        self.btn_connect.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_disconnect = tk.Button(
            btn_frame,
            text="接続解除",
            font=("Meiryo UI", 11, "bold"),
            width=8,
            bg="#374151",
            fg="#ffffff",
            activebackground="#4b5563",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            cursor="hand2",
            state=tk.DISABLED,
            command=self._on_disconnect_clicked,
        )
        self.btn_disconnect.pack(side=tk.LEFT)

        # 下部フッター
        footer = tk.Frame(self.root, bg="#1d232d", height=FOOTER_HEIGHT, padx=16, pady=6)
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        footer.pack_propagate(False)

        self.lbl_status = tk.Label(
            footer,
            text="状態: 未接続",
            font=("Meiryo UI", 11, "bold"),
            fg="#93c5fd",
            bg="#1d232d",
        )
        self.lbl_status.pack(side=tk.LEFT, anchor="center")

        self.lbl_meta = tk.Label(
            footer,
            text="最終受信: --:--:--",
            font=("Meiryo UI", 11),
            fg="#cbd5e1",
            bg="#1d232d",
        )
        self.lbl_meta.pack(side=tk.RIGHT, anchor="center")

        # 中央映像エリア（4:3 横長キャンバス）
        body = tk.Frame(self.root, bg="#14181f", padx=16, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(
            body,
            width=CAMERA_VIEW_WIDTH,
            height=CAMERA_VIEW_HEIGHT,
            bg="#0b0e13",
            highlightthickness=2,
            highlightbackground="#2c3545",
        )
        self.canvas.pack(anchor="center")
        self._draw_placeholder("「接続」ボタンを押して防湿棚カメラに接続してください")

    def _draw_placeholder(self, message: str) -> None:
        self.canvas.delete("all")
        self.canvas.create_text(
            CAMERA_VIEW_WIDTH // 2,
            CAMERA_VIEW_HEIGHT // 2,
            text=message,
            fill="#94a3b8",
            font=("Meiryo UI", 13),
        )

    def _on_connect_clicked(self) -> None:
        self.manual_disconnect = False
        self.disconnect_trigger.clear()
        self.connect_trigger.set()
        self.btn_connect.config(state=tk.DISABLED)
        self.btn_disconnect.config(state=tk.NORMAL)
        self.status_text = "状態: スキャン中..."

    def _on_disconnect_clicked(self) -> None:
        self.manual_disconnect = True
        self.connect_trigger.clear()
        self.disconnect_trigger.set()
        self.btn_connect.config(state=tk.NORMAL)
        self.btn_disconnect.config(state=tk.DISABLED)
        self.status_text = "状態: 切断中..."

    def _ble_thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ble_loop())
        finally:
            loop.close()

    async def _ble_loop(self) -> None:
        while not self.worker_stop.is_set():
            if self.manual_disconnect or not self.connect_trigger.is_set():
                self.status_text = "状態: 未接続"
                await asyncio.sleep(0.15)
                continue

            self.status_text = f"状態: '{DEVICE_NAME}' をスキャン中..."
            device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=4.0)
            if self.manual_disconnect or self.worker_stop.is_set():
                break

            if device is None:
                await asyncio.sleep(0.8)
                continue

            self.status_text = f"状態: 接続中 ({device.address})..."
            self.assembler.reset()

            try:
                async with BleakClient(device, timeout=10.0) as client:
                    self.status_text = f"状態: 接続中 ({DEVICE_NAME})"

                    def on_notify(_sender, data: bytearray) -> None:
                        self.assembler.feed(bytes(data))

                    await client.start_notify(TX_CHAR_UUID, on_notify)
                    while (
                        client.is_connected
                        and not self.worker_stop.is_set()
                        and not self.disconnect_trigger.is_set()
                    ):
                        await asyncio.sleep(0.15)

                    if client.is_connected:
                        try:
                            await client.stop_notify(TX_CHAR_UUID)
                        except Exception:
                            pass
            except Exception as e:
                if not self.manual_disconnect:
                    self.status_text = f"状態: 再試行待ち ({type(e).__name__})"
            finally:
                if self.disconnect_trigger.is_set():
                    self.disconnect_trigger.clear()
                    self.manual_disconnect = True
                    self.connect_trigger.clear()
                    self.status_text = "状態: 未接続"

            if not self.manual_disconnect and not self.worker_stop.is_set():
                await asyncio.sleep(1.0)

    def _render_image(self, bgr_img: np.ndarray) -> None:
        rgb_img = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb_img)

        scale = min(CAMERA_VIEW_WIDTH / pil_img.width, CAMERA_VIEW_HEIGHT / pil_img.height)
        new_w = max(1, int(pil_img.width * scale))
        new_h = max(1, int(pil_img.height * scale))
        resized = pil_img.resize((new_w, new_h), Image.Resampling.BILINEAR)

        self.photo_image = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(
            CAMERA_VIEW_WIDTH // 2,
            CAMERA_VIEW_HEIGHT // 2,
            image=self.photo_image,
            anchor=tk.CENTER,
        )

    def _poll_ui(self) -> None:
        if self.worker_stop.is_set():
            return

        self.lbl_status.config(text=self.status_text)
        self.lbl_meta.config(text=f"最終受信: {self.assembler.last_recv_str}")

        if self.manual_disconnect and self.assembler.latest_img is None:
            self._draw_placeholder("「接続」ボタンを押して防湿棚カメラに接続してください")

        if self.assembler.updated and self.assembler.latest_img is not None:
            self._render_image(self.assembler.latest_img)
            self.assembler.updated = False

        self.root.after(30, self._poll_ui)

    def _on_close(self) -> None:
        self.worker_stop.set()
        self.disconnect_trigger.set()
        if self.ble_thread.is_alive():
            self.ble_thread.join(timeout=2.0)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    HumidityCabinetApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()