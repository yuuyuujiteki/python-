import asyncio
import os
import threading
import tkinter as tk
import customtkinter as ctk
from bleak import BleakClient, BleakScanner
from PIL import Image

# --- BLE設定 ---
SERVICE_UUID = "12345678-1234-1234-1234-1234567890ab"
CHARACTERISTIC_UUID = "87654321-4321-4321-4321-ba0987654321"
DEVICE_NAME = "ESP32S3_Sensor"
LOGO_PATH = "mitsubishi_logo.png"

WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 600

# 明るめスレートトーンのカラーパレット
BG_COLOR = "#1E2634"
GRID_COLOR = "#2A3548"
CARD_BG_COLOR = "#2D3748"
CARD_BORDER_COLOR = "#475569"
LOGO_BADGE_BG = "#FFFFFF"
LOGO_BADGE_BORDER = "#CBD5E1"

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


def load_logo(path: str, height: int = 34):
    if not path or not os.path.exists(path):
        return None
    img = Image.open(path)
    w, h = img.size
    ratio = height / max(h, 1)
    size = (max(int(w * ratio), 24), height)
    return ctk.CTkImage(light_image=img, dark_image=img, size=size)


class GridDistanceApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("ESP32-S3 距離モニター")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.resizable(False, False)

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

        # 2. オーバーレイ用フレーム（背景キャンバス上に配置）
        self.root_overlay = ctk.CTkFrame(self, fg_color="transparent")
        self.root_overlay.place(x=0, y=0, relwidth=1, relheight=1)

        self.logo_image = load_logo(LOGO_PATH, height=34)

        # ヘッダー（ロゴバッジ＋タイトル）
        header_frame = ctk.CTkFrame(self.root_overlay, fg_color="transparent")
        header_frame.pack(pady=(20, 6))

        if self.logo_image is not None:
            logo_badge = ctk.CTkFrame(
                header_frame,
                fg_color=LOGO_BADGE_BG,
                corner_radius=8,
                border_width=1,
                border_color=LOGO_BADGE_BORDER,
            )
            logo_badge.pack(side="left", padx=(0, 14), pady=2)
            ctk.CTkLabel(
                logo_badge,
                image=self.logo_image,
                text="",
            ).pack(padx=10, pady=5)

        ctk.CTkLabel(
            header_frame,
            text="ESP32-S3(VL53L1X) 距離モニター",
            font=ctk.CTkFont(size=36, weight="bold"),
            text_color="#F8FAFC",
        ).pack(side="left")

        # ステータス行（テキスト＋丸インジケーター）
        status_row = ctk.CTkFrame(self.root_overlay, fg_color="transparent")
        status_row.pack(pady=(0, 18))

        self.status_label = ctk.CTkLabel(
            status_row,
            text="ステータス: スキャン中...",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#FBBF24",
        )
        self.status_label.pack(side="left")

        self.status_dot = ctk.CTkCanvas(
            status_row,
            width=20,
            height=20,
            bg=BG_COLOR,
            highlightthickness=0,
            bd=0,
        )
        self.status_dot.pack(side="left", padx=(10, 0), pady=(2, 0))
        self.dot_oval = self.status_dot.create_oval(3, 3, 17, 17, fill="#F59E0B", outline="")

        # 3段カードエリア
        self.cards_frame = ctk.CTkFrame(self.root_overlay, fg_color="transparent")
        self.cards_frame.pack(fill="both", expand=True, padx=32, pady=(0, 32))
        self.cards_frame.grid_columnconfigure((0, 1, 2), weight=1, uniform="sensor")
        self.cards_frame.grid_rowconfigure(0, weight=1)

        self.sensor_widgets = []
        for idx in range(3):
            card = ctk.CTkFrame(
                self.cards_frame,
                corner_radius=18,
                fg_color=CARD_BG_COLOR,
                border_width=1,
                border_color=CARD_BORDER_COLOR,
            )
            card.grid(row=0, column=idx, padx=14, pady=6, sticky="nsew")

            ctk.CTkLabel(
                card,
                text=f"{idx + 1}段目",
                font=ctk.CTkFont(size=24, weight="bold"),
                text_color="#F1F5F9",
            ).pack(pady=(34, 18))

            lbl_dist = ctk.CTkLabel(
                card,
                text="---",
                font=ctk.CTkFont(size=68, weight="bold"),
                text_color="#FFFFFF",
            )
            lbl_dist.pack(pady=(6, 0))

            ctk.CTkLabel(
                card,
                text="cm",
                font=ctk.CTkFont(size=22),
                text_color="#CBD5E1",
            ).pack(pady=(2, 26))

            lbl_state = ctk.CTkLabel(
                card,
                text="待機中",
                font=ctk.CTkFont(size=19, weight="bold"),
                text_color="#F8FAFC",
                fg_color="#475569",
                corner_radius=12,
                width=290,
                height=68,
            )
            lbl_state.pack(pady=(4, 24))

            self.sensor_widgets.append((card, lbl_dist, lbl_state))

        # BLE通信スレッド開始
        self.loop_thread = threading.Thread(target=self.start_ble_thread, daemon=True)
        self.loop_thread.start()

    def _draw_grid(self, step: int = 48, color: str = GRID_COLOR):
        for x in range(0, WINDOW_WIDTH + step, step):
            self.bg_canvas.create_line(x, 0, x, WINDOW_HEIGHT, fill=color, width=1)
        for y in range(0, WINDOW_HEIGHT + step, step):
            self.bg_canvas.create_line(0, y, WINDOW_WIDTH, y, fill=color, width=1)

    def update_sensors_ui(self, parsed_list):
        # parsed_list: [(dist_str, status_str), ...] 長さ3
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

    def _apply_status(self, text: str, text_color: str, dot_color: str):
        self.status_label.configure(text=text, text_color=text_color)
        self.status_dot.itemconfig(self.dot_oval, fill=dot_color)

    def set_connection_status(self, text: str, text_color: str, dot_color: str):
        self.after(0, lambda t=text, tc=text_color, dc=dot_color: self._apply_status(t, tc, dc))

    def start_ble_thread(self):
        asyncio.run(self.ble_main_loop())

    async def ble_main_loop(self):
        while True:
            try:
                self.set_connection_status(
                    f"ステータス: '{DEVICE_NAME}' をスキャン中...", "#FBBF24", "#F59E0B"
                )
                device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=5.0)

                if not device:
                    await asyncio.sleep(2.0)
                    continue

                self.set_connection_status("ステータス: 接続中...", "#FBBF24", "#F59E0B")

                async def notification_handler(sender, data):
                    message = data.decode("utf-8")
                    # フォーマット: "S1:45:FAR|S2:12:NEAR|S3:60:FAR"
                    try:
                        items = message.split("|")
                        parsed = []
                        for item in items:
                            parts = item.split(":")
                            if len(parts) == 3:
                                dist_val = parts[1]
                                status_val = parts[2]
                                parsed.append((dist_val, status_val))
                        if len(parsed) == 3:
                            self.after(0, lambda p=parsed: self.update_sensors_ui(p))
                    except Exception as e:
                        print(f"パースエラー: {e}")

                async with BleakClient(device) as client:
                    self.set_connection_status(
                        "ステータス: 接続完了（測定値を受信中）", "#4ADE80", "#22C55E"
                    )
                    await client.start_notify(CHARACTERISTIC_UUID, notification_handler)

                    while client.is_connected:
                        await asyncio.sleep(1.0)

            except Exception as e:
                print(f"BLE接続エラー: {e}")
                self.set_connection_status(
                    "ステータス: 切断されました（再接続中...）", "#F87171", "#EF4444"
                )
                await asyncio.sleep(3.0)


if __name__ == "__main__":
    app = GridDistanceApp()
    app.mainloop()