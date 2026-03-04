# ハウス開閉器 ROM化・USB分離構成まとめ

作成日時: 2026-02-23 09:11:38

------------------------------------------------------------------------

## 1. 背景

本システムは電源断（コンセント抜去）を前提とした運用環境で使用される。
従来構成ではSDカード上に

-   OS
-   Docker
-   Home Assistant DB

が存在し、電源断による破損リスクが高かった。

そのため以下の方針に変更する。

------------------------------------------------------------------------

## 2. 新構成方針

### SDカード（最小書き込み）

-   Raspberry Pi OS
-   systemd
-   最小ログ（volatile）

### USBフラッシュメモリ

-   Docker data-root
-   Home Assistant config（DB含む）

構成例：

SD: / /boot

USB: /mnt/hausb/docker /mnt/hausb/ha_config

------------------------------------------------------------------------

## 3. 実施内容

### 3.0 USB の UUID 確認とマウントポイント作成

USB を接続した状態で UUID を確認する：

```bash
sudo blkid
# 例: /dev/sda1: UUID="xxxx-xxxx" TYPE="ext4"
```

マウントポイントを作成：

```bash
sudo mkdir -p /mnt/hausb
```

`/etc/fstab` に USB の自動マウントを追記：

```bash
sudo nano /etc/fstab
```

```
# USB フラッシュメモリ（Docker・HA データ用）
UUID=xxxx-xxxx  /mnt/hausb  ext4  defaults,noatime,nofail  0  2
```

> `nofail` を付けることで USB が接続されていなくても起動が止まらなくなります。
> `noatime` で読み取り時の書き込みを抑制し SD カードへの負荷も下げます。

マウントを確認：

```bash
sudo mount -a
df -h | grep hausb
```

------------------------------------------------------------------------

### 3.1 DockerデータをUSBへ移動

```bash
# 1. Docker 停止
sudo systemctl stop docker

# 2. USB へ rsync（進捗表示あり）
sudo rsync -avP /var/lib/docker/ /mnt/hausb/docker/

# 3. daemon.json に data-root 指定
sudo mkdir -p /etc/docker
sudo nano /etc/docker/daemon.json
```

```json
{
  "data-root": "/mnt/hausb/docker"
}
```

```bash
# 4. Docker 再起動
sudo systemctl start docker

# 5. Root Dir 確認（/mnt/hausb/docker と表示されれば OK）
docker info | grep "Docker Root Dir"
```

------------------------------------------------------------------------

### 3.2 Home Assistant config移動

```bash
# 1. HA コンテナ停止
docker stop homeassistant

# 2. USB へコピー
sudo mkdir -p /mnt/hausb/ha_config
sudo cp -a /home/$USER/ha_config/. /mnt/hausb/ha_config/

# 3. コンテナを USB パスで再作成
docker rm homeassistant
docker run -d \
  --name homeassistant \
  --restart unless-stopped \
  --network host \
  -v /mnt/hausb/ha_config:/config \
  ghcr.io/home-assistant/home-assistant:stable
```

------------------------------------------------------------------------

## 4. 自動起動方式の整理

USB 依存構成では **USB マウント後に Docker・HA が起動する**ことを保証する必要があります。

### systemd による管理（推奨）

`/etc/systemd/system/homeassistant.service` を作成：

```ini
[Unit]
Description=Home Assistant
After=network-online.target docker.service
RequiresMountsFor=/mnt/hausb
Requires=docker.service

[Service]
Restart=always
RestartSec=10
ExecStart=/usr/bin/docker start -a homeassistant
ExecStop=/usr/bin/docker stop homeassistant

[Install]
WantedBy=multi-user.target
```

```bash
# Docker の自動再起動ポリシーを無効化（systemd に任せる）
docker update --restart no homeassistant

sudo systemctl daemon-reload
sudo systemctl enable homeassistant
sudo systemctl start homeassistant
```

> `RequiresMountsFor=/mnt/hausb` により、USB がマウントされていない場合はサービスが起動しません。

------------------------------------------------------------------------

## 5. ログおよび書き込み削減

### journald を volatile（メモリのみ）に設定

```bash
sudo nano /etc/systemd/journald.conf
```

```ini
[Journal]
Storage=volatile
```

```bash
sudo systemctl restart systemd-journald
```

### /tmp を tmpfs に

```bash
sudo nano /etc/fstab
```

```
tmpfs  /tmp  tmpfs  defaults,noatime,size=64m  0  0
```

### その他

-   HA recorder 最小化（`HomeAssistantConfiguration.yaml` の `recorder` セクション参照）
-   `/etc/fstab` の SD マウントに `noatime` を追加

これによりSD書き込みを最小化。

------------------------------------------------------------------------

## 6. 期待効果

-   SDカード破損リスク大幅低減
-   Docker破損時もUSB側のみ影響
-   OS自体は生存可能
-   電源断環境での耐久性向上

------------------------------------------------------------------------

## 7. 今後の発展案

-   root read-only化
-   overlayfs導入
-   定期バックアップ自動化
-   SSD化による耐久性向上

------------------------------------------------------------------------

以上
