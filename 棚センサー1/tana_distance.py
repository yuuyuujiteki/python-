import asyncio
from bleak import BleakClient, BleakScanner

# ESP32側で設定したサービスとキャラクタリスティックのUUID
SERVICE_UUID = "12345678-1234-1234-1234-1234567890ab"
CHARACTERISTIC_UUID = "87654321-4321-4321-4321-ba0987654321"
DEVICE_NAME = "ESP32S3_Sensor"


# データを受信したときに呼ばれるコールバック関数
def notification_handler(sender, data):
  message = data.decode("utf-8")
  print(f"[受信データ] {message}")

  if "NEAR" in message:
    print("--> 【警告】30cm以内に障害物があります！")


async def main():
  print(f"'{DEVICE_NAME}' をスキャン中...")

  # デバイスを検索
  device = await BleakScanner.find_device_by_name(DEVICE_NAME)

  if not device:
    print(f"エラー: {DEVICE_NAME} が見つかりませんでした。")
    return

  print(f"接続先が見つかりました: {device.address}")

  async with BleakClient(device) as client:
    print("BLE接続成功！データを受信中...")

    # 通知（Notify）を有効化
    await client.start_notify(CHARACTERISTIC_UUID, notification_handler)

    # 接続を維持してデータを受け取り続ける
    try:
      while True:
        await asyncio.sleep(1)
    except asyncio.CancelledError:
      pass
    finally:
      await client.stop_notify(CHARACTERISTIC_UUID)
      print("切断しました。")


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    print("\nプログラムを終了します。")