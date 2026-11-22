import asyncio
import os
import struct
import threading
import time
import tkinter as tk
import customtkinter as ctk
from bleak import BleakClient, BleakScanner
import cv2
import numpy as np
from PIL import Image, ImageTk

# --- BLE設定（距離センサー） ---
DIST_SERVICE_UUID = "12345678-1234-1234-1234-1234567890ab"
DIST_CHAR_UUID = "87654321-4321-4321-4321-ba0987654321"
DIST_DEVICE_NAME = "ESP32S3_Sensor"

# --- BLE設定（カメラ） ---
CAM_DEVICE_NAME = "ESP32S3-CAM-BLE"
CAM_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
MAX_IMG_BYTES = 500_000

# --- UI設定 ---
LOGO_PATH = "mitsubishi_logo.png"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 640
CAM_PREVIEW_W = 420
CAM_PREVIEW_H = 315

BG_COLOR = "#1E2634"
GRID_COLOR = "#2A3548"
CARD_BG_COLOR = "#2D3748"
CARD_BORDER_COLOR = "#475569"
LOGO_BADGE_BG = "#FFFFFF"
LOGO_BADGE_BORDER = "#CBD5E1"

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


def load_logo(path: str, height: int = 32):
    if not path or not os.path.exists(path):
        return None
    img = Image.open(path)
    w, h = img.size
    ratio = height / max(h, 1)
    size = (max(int(w * ratio), 24), height)
    return ctk.CTkImage(light_image=img, dark_image=img, size=size)


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


class CombinedMonitorApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title("ESP32-S3 距離＆防湿棚カメラモニター")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.resizable(False, False)

        self.assembler = FrameAssembler()
        self.cam_photo_image: ImageTk.PhotoImage | None = None

        self.dist_status_text = f"距離BLE: '{DIST_DEVICE_NAME}' スキャン中..."
        self.cam_status_text = "カメラBLE: 未接続"

        self.cam_manual_disconnect = True
        self.worker_stop = threading.Event()
        self.cam_disconnect_trigger = threading.Event()
        self.cam_connect_trigger = threading.Event()

        self._build_ui()

        self.ble_thread = threading.Thread(target=self._ble_thread_main, daemon=True)
        self.ble_thread.start()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(30, self._poll_camera_ui)

    def _build_ui(self) -> None:
        # 1. グリッド背景キャンバス
        self.bg_canvas = tk.Canvas(
            self,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            bg=BG_COLOR,
            highlightthickness=0,
            bd=0,
        )
        self.bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._draw_grid(step=48, color=GRID_COLOR)

        # 2. オーバーレイフレーム
        self.root_overlay = ctk.CTkFrame(self, fg_color="transparent")
        self.root_overlay.place(x=0, y=0, relwidth=1, relheight=1)

        self.logo_image = load_logo(LOGO_PATH, height=30)

        # ヘッダー（ロゴ＋タイトル＋2系統ステータス）
        header_frame = ctk.CTkFrame(self.root_overlay, fg_color="transparent")
        header_frame.pack(fill="x", padx=24, pady=(16, 10))

        title_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_box.pack(side="left")

        if self.logo_image is not None:
            logo_badge = ctk.CTkFrame(
                title_box,
                fg_color=LOGO_BADGE_BG,
                corner_radius=8,
                border_width=1,
                border_color=LOGO_BADGE_BORDER,
            )
            logo_badge.pack(side="left", padx=(0, 12))
            ctk.CTkLabel(logo_badge, image=self.logo_image, text="").pack(padx=8, pady=4)

        ctk.CTkLabel(
            title_box,
            text="ESP32-S3(VL53L1X) 距離＆防湿棚モニター",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color="#F8FAFC",
        ).pack(side="left")

        status_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        status_box.pack(side="right")

        self.dist_dot = ctk.CTkCanvas(
            status_box, width=16, height=16, bg=BG_COLOR, highlightthickness=0, bd=0
        )
        self.dist_dot.pack(side="left", padx=(0, 6))
        self.dist_dot_oval = self.dist_dot.create_oval(2, 2, 14, 14, fill="#F59E0B", outline="")

        self.dist_status_label = ctk.CTkLabel(
            status_box,
            text=self.dist_status_text,
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#FBBF24",
        )
        self.dist_status_label.pack(side="left", padx=(0, 16))

        self.cam_status_label = ctk.CTkLabel(
            status_box,
            text=self.cam_status_text,
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#93C5FD",
        )
        self.cam_status_label.pack(side="left")

        # メインコンテンツ（左：3段距離カード / 右：防湿棚カメラ）
        main_frame = ctk.CTkFrame(self.root_overlay, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=24, pady=(4, 20))
        main_frame.grid_columnconfigure(0, weight=7, uniform="main")
        main_frame.grid_columnconfigure(1, weight=4, uniform="main")
        main_frame.grid_rowconfigure(0, weight=1)

        # 左：3段距離カード
        cards_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        cards_frame.grid(row=0, column=0, padx=(0, 10), pady=4, sticky="nsew")
        cards_frame.grid_columnconfigure((0, 1, 2), weight=1, uniform="sensor")
        cards_frame.grid_rowconfigure(0, weight=1)

        self.sensor_widgets = []
        for idx in range(3):
            card = ctk.CTkFrame(
                cards_frame,
                corner_radius=18,
                fg_color=CARD_BG_COLOR,
                border_width=1,
                border_color=CARD_BORDER_COLOR,
            )
            card.grid(row=0, column=idx, padx=6, pady=2, sticky="nsew")

            ctk.CTkLabel(
                card,
                text=f"{idx + 1}段目",
                font=ctk.CTkFont(size=22, weight="bold"),
                text_color="#F1F5F9",
            ).pack(pady=(34, 18))

            lbl_dist = ctk.CTkLabel(
                card,
                text="---",
                font=ctk.CTkFont(size=60, weight="bold"),
                text_color="#FFFFFF",
            )
            lbl_dist.pack(pady=(6, 0))

            ctk.CTkLabel(
                card,
                text="cm",
                font=ctk.CTkFont(size=20),
                text_color="#CBD5E1",
            ).pack(pady=(2, 26))

            lbl_state = ctk.CTkLabel(
                card,
                text="待機中",
                font=ctk.CTkFont(size=16, weight="bold"),
                text_color="#F8FAFC",
                fg_color="#475569",
                corner_radius=12,
                width=215,
                height=68,
            )
            lbl_state.pack(pady=(4, 24))

            self.sensor_widgets.append((card, lbl_dist, lbl_state))

        # 右：防湿棚カメラカード
        cam_card = ctk.CTkFrame(
            main_frame,
            corner_radius=18,
            fg_color=CARD_BG_COLOR,
            border_width=1,
            border_color=CARD_BORDER_COLOR,
        )
        cam_card.grid(row=0, column=1, padx=(10, 6), pady=2, sticky="nsew")

        cam_top = ctk.CTkFrame(cam_card, fg_color="transparent")
        cam_top.pack(fill="x", padx=16, pady=(16, 8))

        ctk.CTkLabel(
            cam_top,
            text="防湿棚の状況",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#F1F5F9",
        ).pack(side="left")

        btn_box = ctk.CTkFrame(cam_top, fg_color="transparent")
        btn_box.pack(side="right")

        self.btn_cam_connect = ctk.CTkButton(
            btn_box,
            text="接続",
            width=72,
            height=32,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#2563EB",
            hover_color="#1D4ED8",
            command=self._on_cam_connect_clicked,
        )
        self.btn_cam_connect.pack(side="left", padx=(0, 6))

        self.btn_cam_disconnect = ctk.CTkButton(
            btn_box,
            text="接続解除",
            width=80,
            height=32,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#475569",
            hover_color="#64748B",
            state="disabled",
            command=self._on_cam_disconnect_clicked,
        )
        self.btn_cam_disconnect.pack(side="left")

        self.cam_canvas = tk.Canvas(
            cam_card,
            width=CAM_PREVIEW_W,
            height=CAM_PREVIEW_H,
            bg="#0B0E13",
            highlightthickness=2,
            highlightbackground="#334155",
        )
        self.cam_canvas.pack(padx=16, pady=(6, 10))
        self._draw_cam_placeholder("「接続」を押してカメラBLEに接続")

        self.lbl_cam_meta = ctk.CTkLabel(
            cam_card,
            text="最終受信: --:--:--",
            font=ctk.CTkFont(size=14),
            text_color="#CBD5E1",
        )
        self.lbl_cam_meta.pack(pady=(0, 14))

    def _draw_grid(self, step: int = 48, color: str = GRID_COLOR) -> None:
        for x in range(0, WINDOW_WIDTH + step, step):
            self.bg_canvas.create_line(x, 0, x, WINDOW_HEIGHT, fill=color, width=1)
        for y in range(0, WINDOW_HEIGHT + step, step):
            self.bg_canvas.create_line(0, y, WINDOW_WIDTH, y, fill=color, width=1)

    def _draw_cam_placeholder(self, message: str) -> None:
        self.cam_canvas.delete("all")
        self.cam_canvas.create_text(
            CAM_PREVIEW_W // 2,
            CAM_PREVIEW_H // 2,
            text=message,
            fill="#94A3B8",
            font=("Meiryo UI", 12),
        )

    def update_sensors_ui(self, parsed_list) -> None:
        for idx, (dist_str, status_str) in enumerate(parsed_list):
            if idx >= len(self.sensor_widgets):
                break
            card, lbl_dist, lbl_state = self.sensor_widgets[idx]
            lbl_dist.configure(text=dist_str)

            if status_str == "NEAR":
                card.configure(border_color="#F59E0B")
                lbl_state.configure(
                    text="⚠  棚がいっぱいだよ！\n取りに来て！",
                    fg_color="#F59E0B",
                    text_color="#111827",
                )
            elif status_str == "FAR":
                card.configure(border_color="#3B82F6")
                lbl_state.configure(
                    text="✓  まだまだ空いてます。",
                    fg_color="#1D4ED8",
                    text_color="#FFFFFF",
                )
            else:
                card.configure(border_color="#EF4444")
                lbl_state.configure(
                    text="✕  測定エラー",
                    fg_color="#B91C1C",
                    text_color="#FFFFFF",
                )

    def _apply_dist_status(self, text: str, text_color: str, dot_color: str) -> None:
        self.dist_status_label.configure(text=text, text_color=text_color)
        self.dist_dot.itemconfig(self.dist_dot_oval, fill=dot_color)

    def set_dist_status(self, text: str, text_color: str, dot_color: str) -> None:
        self.after(0, lambda t=text, tc=text_color, dc=dot_color: self._apply_dist_status(t, tc, dc))

    def _on_cam_connect_clicked(self) -> None:
        self.cam_manual_disconnect = False
        self.cam_disconnect_trigger.clear()
        self.cam_connect_trigger.set()
        self.btn_cam_connect.configure(state="disabled")
        self.btn_cam_disconnect.configure(state="normal")
        self.cam_status_text = f"カメラBLE: '{CAM_DEVICE_NAME}' スキャン中..."

    def _on_cam_disconnect_clicked(self) -> None:
        self.cam_manual_disconnect = True
        self.cam_connect_trigger.clear()
        self.cam_disconnect_trigger.set()
        self.btn_cam_connect.configure(state="normal")
        self.btn_cam_disconnect.configure(state="disabled")
        self.cam_status_text = "カメラBLE: 切断中..."

    def _ble_thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_both_ble_loops())
        finally:
            loop.close()

    async def _run_both_ble_loops(self) -> None:
        await asyncio.gather(
            self._distance_ble_loop(),
            self._camera_ble_loop(),
        )

    async def _distance_ble_loop(self) -> None:
        while not self.worker_stop.is_set():
            try:
                self.set_dist_status(
                    f"距離BLE: '{DIST_DEVICE_NAME}' スキャン中...", "#FBBF24", "#F59E0B"
                )
                device = await BleakScanner.find_device_by_name(DIST_DEVICE_NAME, timeout=5.0)
                if self.worker_stop.is_set():
                    break

                if not device:
                    await asyncio.sleep(2.0)
                    continue

                self.set_dist_status("距離BLE: 接続中...", "#FBBF24", "#F59E0B")

                async def notification_handler(_sender, data) -> None:
                    message = data.decode("utf-8")
                    # フォーマット: "S1:45:FAR|S2:12:NEAR|S3:60:FAR"
                    try:
                        items = message.split("|")
                        parsed = []
                        for item in items:
                            parts = item.split(":")
                            if len(parts) == 3:
                                parsed.append((parts[1], parts[2]))
                        if len(parsed) == 3:
                            self.after(0, lambda p=parsed: self.update_sensors_ui(p))
                    except Exception as e:
                        print(f"距離パースエラー: {e}")

                async with BleakClient(device) as client:
                    self.set_dist_status("距離BLE: 接続完了", "#4ADE80", "#22C55E")
                    await client.start_notify(DIST_CHAR_UUID, notification_handler)

                    while client.is_connected and not self.worker_stop.is_set():
                        await asyncio.sleep(1.0)

            except Exception as e:
                if not self.worker_stop.is_set():
                    print(f"距離BLE接続エラー: {e}")
                    self.set_dist_status("距離BLE: 再接続中...", "#F87171", "#EF4444")
                    await asyncio.sleep(3.0)

    async def _camera_ble_loop(self) -> None:
        while not self.worker_stop.is_set():
            if self.cam_manual_disconnect or not self.cam_connect_trigger.is_set():
                self.cam_status_text = "カメラBLE: 未接続"
                await asyncio.sleep(0.15)
                continue

            self.cam_status_text = f"カメラBLE: '{CAM_DEVICE_NAME}' スキャン中..."
            device = await BleakScanner.find_device_by_name(CAM_DEVICE_NAME, timeout=4.0)
            if self.cam_manual_disconnect or self.worker_stop.is_set():
                break

            if device is None:
                await asyncio.sleep(0.8)
                continue

            self.cam_status_text = f"カメラBLE: 接続中 ({device.address})..."
            self.assembler.reset()

            try:
                async with BleakClient(device, timeout=10.0) as client:
                    self.cam_status_text = f"カメラBLE: 接続完了 ({CAM_DEVICE_NAME})"

                    def on_notify(_sender, data: bytearray) -> None:
                        self.assembler.feed(bytes(data))

                    await client.start_notify(CAM_TX_CHAR_UUID, on_notify)
                    while (
                        client.is_connected
                        and not self.worker_stop.is_set()
                        and not self.cam_disconnect_trigger.is_set()
                    ):
                        await asyncio.sleep(0.15)

                    if client.is_connected:
                        try:
                            await client.stop_notify(CAM_TX_CHAR_UUID)
                        except Exception:
                            pass
            except Exception as e:
                if not self.cam_manual_disconnect:
                    self.cam_status_text = f"カメラBLE: 再試行待ち ({type(e).__name__})"
            finally:
                if self.cam_disconnect_trigger.is_set():
                    self.cam_disconnect_trigger.clear()
                    self.cam_manual_disconnect = True
                    self.cam_connect_trigger.clear()
                    self.cam_status_text = "カメラBLE: 未接続"

            if not self.cam_manual_disconnect and not self.worker_stop.is_set():
                await asyncio.sleep(1.0)

    def _render_camera_image(self, bgr_img: np.ndarray) -> None:
        rgb_img = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb_img)

        scale = min(CAM_PREVIEW_W / pil_img.width, CAM_PREVIEW_H / pil_img.height)
        new_w = max(1, int(pil_img.width * scale))
        new_h = max(1, int(pil_img.height * scale))
        resized = pil_img.resize((new_w, new_h), Image.Resampling.BILINEAR)

        self.cam_photo_image = ImageTk.PhotoImage(resized)
        self.cam_canvas.delete("all")
        self.cam_canvas.create_image(
            CAM_PREVIEW_W // 2,
            CAM_PREVIEW_H // 2,
            image=self.cam_photo_image,
            anchor=tk.CENTER,
        )

    def _poll_camera_ui(self) -> None:
        if self.worker_stop.is_set():
            return

        self.cam_status_label.configure(text=self.cam_status_text)
        self.lbl_cam_meta.configure(text=f"最終受信: {self.assembler.last_recv_str}")

        if self.cam_manual_disconnect and self.assembler.latest_img is None:
            self._draw_cam_placeholder("「接続」を押してカメラBLEに接続")

        if self.assembler.updated and self.assembler.latest_img is not None:
            self._render_camera_image(self.assembler.latest_img)
            self.assembler.updated = False

        self.after(30, self._poll_camera_ui)

    def _on_close(self) -> None:
        self.worker_stop.set()
        self.cam_disconnect_trigger.set()
        if self.ble_thread.is_alive():
            self.ble_thread.join(timeout=2.0)
        self.destroy()


def main() -> None:
    app = CombinedMonitorApp()
    app.mainloop()


if __name__ == "__main__":
    main()