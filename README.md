# ビニールハウス自動開閉制御システム

Raspberry Pi + Home Assistant を使ったビニールハウス開閉器の制御システムです。
スマートフォンから遠隔操作でき、温湿度・CO2 を常時モニタリングできます。

> **2026-03-01 メイカーズ長岡まつり 展示作品**
> OpenAgriTech

---

## 概要

| 項目 | 内容 |
|------|------|
| プラットフォーム | Raspberry Pi OS (Trixie) |
| UI | Home Assistant (Docker) |
| 外部公開 | Cloudflare Tunnel |
| センサ | SEN0501（温湿度）、SCD41（CO2・温湿度） |
| モータドライバ | Cytron MDD10 |
| モータ制御 | PWM 20kHz（GPIO18）+ 方向制御（GPIO23） |

---

## システム構成

```
スマートフォン
    ↓ HTTPS
Cloudflare Tunnel
    ↓
Home Assistant（Docker, host network）
    ↓ REST API（127.0.0.1）
┌───────────────────┬──────────────────────┐
│ metrics_server.py │   motor_server.py    │
│   :8124           │   :8125              │
│   温湿度・CO2取得  │   モータ開閉制御      │
└────────┬──────────┴──────────┬───────────┘
         │ I2C                 │ GPIO / PWM
    SEN0501 / SCD41         MDD10 → モータ
```

---

## ファイル構成

```
.
├── README.md                          # このファイル
├── docs/
│   ├── PARTS_LIST.md                  # パーツリスト（BOM）
│   └── SETUP.md                       # セットアップ手順
└── src/
    ├── metrics_server.py              # 環境センサ HTTP サーバ（:8124）
    ├── motor_server.py                # モータ制御 HTTP サーバ（:8125）
    ├── HomeAssistantConfiguration.yaml# Home Assistant 設定
    ├── SPEC.md                        # システム仕様書（詳細）
    └── ROM_USB_MIGRATION_PLAN.md      # SDカードROM化・USB分離計画
```

---

## 主な機能

- **遠隔開閉操作**: スマートフォンから「開く」「閉じる」「停止」ボタン操作
- **動作時間・出力の調整**: UI スライダーで秒数（1〜900秒）と PWM duty（0〜100%）を設定
- **環境モニタリング**: 温度・湿度（SEN0501）、CO2・温度・湿度（SCD41）を 30 秒間隔で取得
- **フェイルセーフ**: 最大 900 秒制限・ロックファイルによる二重起動防止・緊急停止 API

---

## セットアップ

詳細は [docs/SETUP.md](docs/SETUP.md) を参照してください。

### 必要なパーツ

[docs/PARTS_LIST.md](docs/PARTS_LIST.md) を参照してください。

### 依存ライブラリ（Python）

```bash
pip install RPi.GPIO adafruit-circuitpython-scd4x dfrobot-environmental-sensor
```

### サービス起動（systemd）

```bash
# metrics_server を登録・起動
sudo systemctl enable metrics_server
sudo systemctl start metrics_server

# motor_server を登録・起動
sudo systemctl enable motor_server
sudo systemctl start motor_server
```

---

## ネットワーク設計

- Home Assistant と各サーバはすべて `127.0.0.1` で通信
- LAN の IP アドレス変更やテザリング切り替えに依存しない設計
- 外部公開は Cloudflare Tunnel のみ（ポート開放不要）

---

## 耐障害性（ROM化・USB分離）

電源断を前提とした農業現場での運用のため、以下の対策を実施しています：

- root を `ro`（読み取り専用）でマウントし SD カードへの書き込みをゼロに
- journald を volatile（メモリのみ）、`/tmp` を tmpfs に
- Docker データ・Home Assistant DB・cloudflared データを USB フラッシュメモリへ分離
- 詳細: [src/ROM_USB_MIGRATION_PLAN.md](src/ROM_USB_MIGRATION_PLAN.md)

---

## ライセンス

MIT License

---

## 作成について

本プロジェクトのコード・ドキュメントは、**Claude（Anthropic 社製 AI アシスタント）の支援を受けて作成**しました。

- コード生成・レビュー・ドキュメント整備に Claude Code（claude-sonnet-4-6）、chatGPT5.2を使用
- 設計方針・要件定義・最終確認は人間（iga）が行っています

> *AI を農業 × ものづくりにどう活かすか、という実験でもあります。*
