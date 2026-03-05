# セットアップ手順

ビニールハウス自動開閉制御システム

> このドキュメントは Claude（Anthropic）の支援を受けて作成しました。

---

## 前提条件

- Raspberry Pi に **Raspberry Pi OS (Trixie)** がインストール済み
- インターネット接続済み
- `sudo` 権限あり
- I2C を有効化済み（手順は下記「0-b」参照）

> **Trixie と GPIO ライブラリについて**
>
> 2025年以降、Raspberry Pi OS の標準リリースは Trixie (Debian 13) になりました。
> Trixie 環境では従来よく使われていた `pigpio` が **実質使用不可**です：
>
> - **Pi 5 で動作しない（アーキテクチャ的問題）**: Pi5 は BCM2712 + **RP1 サウスブリッジ**構成で、GPIO を管理するのは RP1 チップ。pigpio は `/dev/mem` 経由の DMA 直アクセスを前提としており、この設計が RP1 構成では根本的に機能しない。pigpio 開発者は Pi5 対応を「互換性を壊さずには不可能かもしれない巨大なタスク」と GitHub Issue で明言しており、ロードマップなし（[#589](https://github.com/joan2937/pigpio/issues/589)）
> - **Trixie でビルド不可（Python の問題）**: pigpio の `setup.py` が `distutils` に依存。Python 3.12 で `distutils` が標準ライブラリから削除された。Trixie には代替の `python3-distutils` パッケージもなく、ビルドが通らない
> - **メンテナンス停滞**: 上記問題を抱えたまま更新が止まっており、今後の解決も見込めない
>
> 本システムはこれらを踏まえ、ライブラリに依存しないカーネル標準の
> **sysfs PWM インターフェース**を採用しています。
> （参考スレッド: https://forums.raspberrypi.com/viewtopic.php?t=392438 ）

---

## 0-a. ハードウェア PWM の有効化（必須）

GPIO18 でハードウェア PWM を使うために、起動設定ファイルに `dtoverlay=pwm` を追加します。

```bash
sudo nano /boot/firmware/config.txt
```

末尾に以下を追記：

```
# ハードウェアPWM有効化 (GPIO18=PWM0)
dtoverlay=pwm
```

追記後、再起動：

```bash
sudo reboot
```

再起動後に確認：

```bash
ls /sys/class/pwm/pwmchip0
# → export  npwm  power  subsystem  uevent が表示されれば OK
```

> **なぜ sysfs PWM を使うのか**
>
> `pigpio` は以下の理由で Trixie / Pi5 では使用不可です：
>
> | 問題 | 原因 |
> |------|------|
> | Pi5 で動作しない | Pi5 は BCM2712 + **RP1 サウスブリッジ**構成。GPIO を管理するのは RP1 であり、pigpio が依存する `/dev/mem` 経由の DMA アクセスが機能しない。pigpio 開発者自身が「互換性を壊さずには不可能かもしれない」と明言し、Pi5 対応ロードマップなし |
> | Trixie でビルド不可 | pigpio の `setup.py` が `distutils` に依存。Python 3.12 で `distutils` が標準ライブラリから削除され、Trixie には代替パッケージもない |
> | メンテナンス事実上終了 | 上記問題を抱えたまま更新停止状態 |
>
> カーネルの PWM サブシステム (`/sys/class/pwm/`) を直接操作する方式はライブラリ依存がなく、Pi4・Pi5 両方で動作します。
>
> **Trixie 以降での GPIO ライブラリ全体像（参考）:**
>
> | ライブラリ | Pi4 | Pi5 | PWM | 用途 |
> |-----------|:---:|:---:|-----|------|
> | `pigpio` | ○ | **×** | ハードウェア | 実質廃止 |
> | `RPi.GPIO` | ○ | △ | ソフトのみ | 非推奨移行中（Pi5は非公式） |
> | `rpi-lgpio` | ○ | ○ | ソフトのみ | RPi.GPIO の drop-in 代替 |
> | `lgpio` | ○ | ○ | ソフトのみ | Raspberry Pi 公式推奨 |
> | `gpiozero` | ○ | ○ | ソフトのみ | 公式推奨（lgpio バックエンド） |
> | `python-gpiod` | ○ | ○ | なし | Linux 標準 GPIO のみ |
> | **sysfs PWM** | **○** | **○** | **ハードウェア** | **精度 PWM に最善** |
>
> モータ制御のように精度が必要な PWM には、現時点では **sysfs ハードウェア PWM が最も安定**しています。
>
> **Pi5 利用者へ**: `RPi.GPIO` の代わりに `pip install rpi-lgpio` をインストールすると、コードを変更せずに GPIO 方向制御を Pi5 でも動作させられます。`pwmchip0` は Pi4・Pi5 とも `dtoverlay=pwm` で同様に使用できます。
>
> ※現時点 2026/03 での情報です。詳しい背景情報は作者(iga)も正しく把握できていません。

---

## 0-b. I2C の有効化（必須）

センサ（SEN0501 / SCD41）は I2C 接続のため、有効化が必要です。

```bash
sudo raspi-config
```

`raspi-config` のメニューで操作：

```
3 Interface Options
  → I5 I2C
    → Yes（有効化）
    → OK
```

設定後に再起動（PWM 設定と同時に行う場合は一度でまとめて OK）：

```bash
sudo reboot
```

再起動後に確認：

```bash
i2cdetect -y 1
# → 0x22（SEN0501）と 0x62（SCD41）が表示されれば OK
```

---

## 1. Python 依存ライブラリのインストール

Trixie（Python 3.12）では `pip` のシステム全体インストールが制限されています。
`sudo` で強制インストールするか、仮想環境を使います。

**方法 A（推奨）: `--break-system-packages` フラグで直接インストール**

```bash
sudo pip install RPi.GPIO adafruit-circuitpython-scd4x dfrobot-environmental-sensor --break-system-packages
```

> systemd サービスが `User=root` で動作するため、root の環境にインストールする必要があります。
> venv を使う場合は systemd の `ExecStart` を venv のパスに変更してください。

**方法 B: venv を使う場合**

```bash
sudo python3 -m venv /opt/house-controller/.venv
sudo /opt/house-controller/.venv/bin/pip install RPi.GPIO adafruit-circuitpython-scd4x dfrobot-environmental-sensor
```

venv を使う場合は後述の systemd サービスの `ExecStart` を以下に変更：

```
ExecStart=/opt/house-controller/.venv/bin/python3 /opt/house-controller/metrics_server.py
```

---

## 2. ファイルの配置

```bash
# 任意のディレクトリに配置（例: /opt/house-controller）
sudo mkdir -p /opt/house-controller
sudo cp metrics_server.py /opt/house-controller/
sudo cp motor_server.py /opt/house-controller/
```

---

## 3. systemd サービスの設定

### metrics_server（センササーバ）

```bash
sudo nano /etc/systemd/system/metrics_server.service
```

```ini
[Unit]
Description=House Metrics Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/house-controller/metrics_server.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

### motor_server（モータ制御サーバ）

```bash
sudo nano /etc/systemd/system/motor_server.service
```

```ini
[Unit]
Description=House Motor Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/house-controller/motor_server.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

### サービスの有効化・起動

```bash
sudo systemctl daemon-reload
sudo systemctl enable metrics_server motor_server
sudo systemctl start metrics_server motor_server

# 動作確認
sudo systemctl status metrics_server
sudo systemctl status motor_server
```

---

## 4. 動作確認（ローカル）

```bash
# センサデータの確認
curl http://127.0.0.1:8124/metrics

# モータサーバのヘルスチェック
curl http://127.0.0.1:8125/health

# モータ動作テスト（開く方向、5秒、duty 50%）
curl "http://127.0.0.1:8125/run?dir=0&sec=5&duty=50"

# 緊急停止
curl http://127.0.0.1:8125/stop
```

---

## 5. Docker / Home Assistant のセットアップ

### Docker インストール

```bash
curl -sSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

### Home Assistant 起動

```bash
docker run -d \
  --name homeassistant \
  --restart unless-stopped \
  --network host \
  -v /home/$USER/ha_config:/config \
  ghcr.io/home-assistant/home-assistant:stable
```

### Home Assistant 設定の適用

`HomeAssistantConfiguration.yaml` の内容を `/home/$USER/ha_config/configuration.yaml` に追記または上書きします。

```bash
cp HomeAssistantConfiguration.yaml ~/ha_config/configuration.yaml
```

その後 Home Assistant を再起動：

```bash
docker restart homeassistant
```

---

## 6. Cloudflare Tunnel の設定

### 6.1 ドメインの取得（必須）

Cloudflare Tunnel を使うには独自ドメインが必要です。
Cloudflare でドメインを登録・管理すると Tunnel との連携がスムーズです。

**コスト例（Cloudflare Registrar）:**

| TLD | 年額（目安） |
|-----|-------------|
| `.party` | 約 $4 / 年 ← 最安クラス |
| `.com` | 約 $10 / 年 |
| `.dev` | 約 $12 / 年 |

> `.party` ドメインが現状(2026/03/03)で最安クラスです。
> Cloudflare Registrar は原価販売のため更新料も同額です。

**手順:**
1. [Cloudflare](https://www.cloudflare.com/) にアカウント登録
2. `Domain Registration` → `Register Domains` でドメインを検索・購入
3. 購入後、自動的に Cloudflare DNS で管理される
※もっと安いのがあればすみません。なお無料でやる方法もありますが、手順がまあまあ伸びるので端折ります。

### 6.2 cloudflared のインストール（Raspberry Pi 上）

**root `ro` 化より前に実施すること。**

```bash
# Cloudflare GPG キーを追加
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null

# apt リポジトリを追加
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list

# インストール
sudo apt-get update && sudo apt-get install cloudflared
```

> root `ro` 化後にアップデートする場合は `sudo mount -o remount,rw /` で一時的に rw に戻してから実施してください。

### 6.3 Tunnel の作成と設定

1. [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) にログイン
2. `Networks` → `Connectors` → `Create a Tunnel` → `Cloudflared` を選択
3. Tunnel 名を入力
4. **「Install and run a connector」** の画面に表示されるコマンドを Raspberry Pi で実行

   ```bash
   # 表示されるコマンド例（トークンは各自異なる）
   sudo cloudflared service install <YOUR_TUNNEL_TOKEN>
   ```

   > このコマンドで cloudflared が systemd サービスとして登録・起動されます。

5. Tunnel のルーティング設定：
   - **Service**: `http://localhost:8123`
   - **Domain**: 取得したドメインのサブドメイン（例: `ha.yourdomain.party`）

6. `HomeAssistantConfiguration.yaml` の `external_url` を設定した URL に変更して HA を再起動

### 6.4 動作確認

```bash
# cloudflared サービスの状態確認
sudo systemctl status cloudflared

# ブラウザまたはスマートフォンから設定した URL にアクセスして HA が表示されれば OK
```

---

## 7. USB 分離構成（root ro 化時は必須・推奨）

root を ro 化している場合は Docker・HA・cloudflared データの USB 分離が必須です（ro のまま書き込めないため）。
SD カードの長寿命化にも効果があります。
手順は [src/ROM_USB_MIGRATION_PLAN.md](../src/ROM_USB_MIGRATION_PLAN.md) を参照してください。

---

## トラブルシューティング

| 症状 | 確認事項 |
|------|---------|
| センサ値が取れない | `i2cdetect -y 1` で 0x22 / 0x62 が見えるか確認 |
| モータが動かない | GPIO18 / GPIO23 の配線・MDD10 の電源を確認 |
| PWM が動作しない | `ls /sys/class/pwm/pwmchip0` でディレクトリが存在するか確認。なければ `dtoverlay=pwm` が未設定 |
| HA から REST が失敗する | `curl http://127.0.0.1:8125/health` を Raspberry Pi 上で実行 |
| SCD41 が `null` を返す | 起動直後は `data_ready` が false。数秒待ってから再取得 |
| cloudflared が起動しない | `sudo systemctl status cloudflared` でログ確認。トークンが正しいか再確認 |
| pip install が失敗する | `--break-system-packages` フラグを追加、または venv を使用 |
| **Pi5 でモータが動かない（GPIO エラー）** | RPi.GPIO は Pi5 非公式。`pip install rpi-lgpio --break-system-packages` を追加インストールすると drop-in 代替として動作する |
| **Pi5 で pigpio エラー** | pigpio は Pi5 非対応（RP1 チップの DMA 互換性なし）。本システムは pigpio を使用していないため通常は出ないが、出た場合は pigpio の残骸を削除する |
