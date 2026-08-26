#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kabu screener — J-Quants API v2 / Free プラン前提のバリュー x クオリティ スクリーナー

旧版 (「割安な中からモメンタムの外れ値を除外する」) からの主な変更点
--------------------------------------------------------------------
1. 逐次ハードフィルタ -> 合成スコア方式
   PER/PBR で足切りしてから並べるのではなく、バリュー / クオリティ /
   モメンタム / 低リスクの 4 つを業種内順位スコア (rank-based z) にして
   加重合成する。閾値をまたいだ瞬間に銘柄が消える不安定さを無くす。

2. バリュートラップ対策としてクオリティを明示的に入れた
   自己資本比率・ROE・営業利益率・アクルーアル (NP-CFO)/TA。
   「安いだけの壊れた会社」が上位に来るのを抑える。

3. モメンタム除外を「固定シグマ」から「クロスセクション百分位」に変更
   下位 N%   = 落ちるナイフ (バリュートラップの本体) -> 除外
   上位 M%   = 急騰・材料株で入れない          -> 除外
   除外銘柄も出力には残し、理由を ExcludeReason に書く (監査できる)。

4. モメンタムは 12-1 (直近 1 か月を飛ばした 12 か月リターン)
   直近 1 か月は短期反転が混ざるので除く。教科書どおりの定義に直した。

5. 分割・併合の調整を厳密化
   リターンは AdjC ベース。EPS/BPS など 1 株あたり指標は開示日と評価日の
   調整係数比 (k_disc / k_asof) を掛けてから株価と比較する。

6. Free プランを前提にした取得設計
   - 12 週遅延を自動検出して評価基準日 (asof) を決める
   - 全銘柄を 1 リクエストで返す date 指定を使い、銘柄ループを避ける
   - 直近は日次・過去は週次サンプリングで価格リクエストを ~100 本に圧縮
   - 取得結果は日付単位でディスクキャッシュ。2 回目以降は差分だけ取る

7. 増配判定を「予想 vs 前期実績」と「予想の上方修正」の 2 本立てに
   DivIncrease  : 今期予想年間配当 > 前期実績年間配当
   DivRevisedUp : 同一決算期の予想配当が前回開示から引き上げられた

出力 (SCREENER_OUT, 既定 ./out)
  screen_<asof>.csv       全ユニバース + 全ファクタ + 除外理由
  screen_<asof>_top.csv   最終上位 N 銘柄
  report_<asof>.md        人間が読むレポート

使い方
  set JQUANTS_API_KEY=...        (https://jpx-jquants.com/ja/dashboard で発行)
  python screener.py                    # 通常実行
  python screener.py --quick            # 財務の取得期間を短縮した軽量実行
  python screener.py --self-test        # ネットワーク不要。ロジックの自己検証
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Optional, Sequence

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

API_BASE = "https://api.jquants.com/v2"
DASHBOARD_URL = "https://jpx-jquants.com/ja/dashboard"
SPEC_URL = "https://jpx-jquants.com/ja/spec"
MIGRATION_URL = "https://jpx-jquants.com/ja/spec/migration-v1-v2"

# 市場区分コード (eq-master の Mkt)
MARKET_NAMES = {
    "0111": "プライム",
    "0112": "スタンダード",
    "0113": "グロース",
    "0105": "TOKYO PRO MARKET",
    "0109": "その他",
}
DEFAULT_MARKETS = ("0111", "0112", "0113")

# Free プラン: 直近 12 週は取得できず、履歴は約 2 年
FREE_DELAY_DAYS = 84
FREE_HISTORY_DAYS = 730

LOG = logging.getLogger("screener")


class JQuantsError(RuntimeError):
    """J-Quants API 由来のエラー (HTTP ステータスとメッセージを保持する)。"""

    def __init__(self, message: str, status: Optional[int] = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


@dataclass
class Config:
    api_key: str = ""
    out_dir: Path = Path("out")
    cache_dir: Path = Path("cache")

    asof: Optional[str] = None          # 明示指定しなければ自動検出
    markets: tuple[str, ...] = DEFAULT_MARKETS

    # --- データ取得量 -------------------------------------------------------
    dense_days: int = 75                # 直近この日数は日次で取る (流動性/ボラ用)
    hist_days: int = 400                # モメンタム用に遡る日数 (12 か月 + 余裕)
    weekly_step: int = 7                # 過去区間のサンプリング間隔 (日)
    fins_days: int = 460                # 財務サマリを遡る日数 (前期実績を拾うため)

    # --- ハードゲート -------------------------------------------------------
    min_turnover: float = 30_000_000.0  # 売買代金の中央値 (円/日)
    min_equity_ratio: float = 15.0      # 自己資本比率 (%)
    min_price_points: int = 30          # 価格サンプルの最低本数

    # --- モメンタム除外 -----------------------------------------------------
    mom_low_pct: float = 10.0           # この百分位未満 = 落ちるナイフ
    mom_high_pct: float = 98.0          # この百分位超  = 急騰・材料株

    # --- スコア重み ---------------------------------------------------------
    w_value: float = 1.0
    w_quality: float = 0.8
    w_momentum: float = 0.4
    w_lowrisk: float = 0.2

    top_n: int = 30
    sector_neutral: bool = True
    min_sector_size: int = 8            # これ未満の業種は全体平均でスコア化

    # --- 通信 ---------------------------------------------------------------
    max_workers: int = 4
    min_interval: float = 0.12          # リクエスト間の最小間隔 (秒)
    max_retries: int = 5
    timeout: int = 30
    no_cache: bool = False


def build_config(args: argparse.Namespace) -> Config:
    cfg = Config()
    cfg.api_key = os.environ.get("JQUANTS_API_KEY", "").strip()

    base = Path(__file__).resolve().parent
    out_env = os.environ.get("SCREENER_OUT", "").strip()
    cfg.out_dir = Path(out_env) if out_env else base / "out"
    cache_env = os.environ.get("SCREENER_CACHE", "").strip()
    cfg.cache_dir = Path(cache_env) if cache_env else base / "cache"

    if args.out:
        cfg.out_dir = Path(args.out)
    if args.cache:
        cfg.cache_dir = Path(args.cache)
    if args.asof:
        cfg.asof = normalize_date(args.asof)
    if args.markets:
        cfg.markets = tuple(m.strip() for m in args.markets.split(",") if m.strip())
    if args.top_n:
        cfg.top_n = args.top_n
    if args.min_turnover is not None:
        cfg.min_turnover = args.min_turnover
    if args.no_cache:
        cfg.no_cache = True
    if args.no_sector_neutral:
        cfg.sector_neutral = False
    if args.quick:
        # 財務は直近 5 か月だけ = 四半期開示 1 巡分。前期実績が拾えないので
        # 増配判定は予想の上方修正のみになる。初回の試し撃ち向け。
        cfg.fins_days = 150
        cfg.hist_days = 400
    if args.min_equity_ratio is not None:
        cfg.min_equity_ratio = args.min_equity_ratio
    if args.mom_low_pct is not None:
        cfg.mom_low_pct = args.mom_low_pct
    if args.mom_high_pct is not None:
        cfg.mom_high_pct = args.mom_high_pct
    if cfg.mom_low_pct >= cfg.mom_high_pct:
        raise SystemExit("--mom-low-pct は --mom-high-pct より小さくしてください")
    for name in ("w_value", "w_quality", "w_momentum", "w_lowrisk"):
        val = getattr(args, name, None)
        if val is not None:
            setattr(cfg, name, val)
    return cfg


def normalize_date(value: str) -> str:
    """YYYYMMDD / YYYY-MM-DD のどちらでも受けて YYYY-MM-DD に正規化する。"""
    v = value.strip().replace("/", "-")
    if len(v) == 8 and v.isdigit():
        return f"{v[:4]}-{v[4:6]}-{v[6:]}"
    datetime.strptime(v, "%Y-%m-%d")  # 妥当性チェック
    return v


# ---------------------------------------------------------------------------
# API クライアント
# ---------------------------------------------------------------------------


class JQuantsClient:
    """J-Quants API v2 の薄いクライアント。

    - 認証は x-api-key ヘッダ (v2 で メール/パスワード方式は廃止)
    - pagination_key を辿って全ページ連結
    - date 指定のレスポンスは日付単位で gzip JSON にキャッシュする。
      空レスポンス (休場日など) も「空」として記録し、再取得しない。
    """

    def __init__(self, cfg: Config) -> None:
        if not cfg.api_key:
            raise JQuantsError(
                "環境変数 JQUANTS_API_KEY が設定されていません。\n"
                f"  {DASHBOARD_URL} で API キーを発行し、JQUANTS_API_KEY に設定してください。\n"
                "  PowerShell 例:\n"
                "    [Environment]::SetEnvironmentVariable('JQUANTS_API_KEY','<key>','User')\n"
                "    $env:JQUANTS_API_KEY = [Environment]::GetEnvironmentVariable('JQUANTS_API_KEY','User')"
            )
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": cfg.api_key,
            "User-Agent": "kabu-screener/2.0",
            "Accept": "application/json",
        })
        self._last_call = 0.0
        self.request_count = 0
        self.cache_hits = 0

    # -- 低レベル ----------------------------------------------------------
    def _throttle(self) -> None:
        wait = self.cfg.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(self.cfg.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.cfg.timeout)
                self.request_count += 1
            except requests.RequestException as exc:  # ネットワーク断など
                last_exc = exc
                LOG.warning("通信エラー (%s/%s): %s", attempt + 1, self.cfg.max_retries, exc)
                time.sleep(delay + random.random() * 0.3)
                delay = min(delay * 2, 30)
                continue

            if resp.status_code == 200:
                return resp.json()

            body = _error_message(resp)
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                sleep_for = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() else delay
                LOG.warning(
                    "HTTP %s (%s/%s) %s — %.1fs 待機して再試行",
                    resp.status_code, attempt + 1, self.cfg.max_retries, body, sleep_for,
                )
                time.sleep(sleep_for + random.random() * 0.3)
                delay = min(delay * 2, 60)
                continue

            # 恒久的なエラーは即座に投げる (401/403/410 など)
            raise JQuantsError(
                _explain_status(resp.status_code, body, url), status=resp.status_code, url=url
            )

        if last_exc is not None:
            raise JQuantsError(f"通信に失敗しました: {last_exc}", url=url)
        raise JQuantsError(f"リトライ上限に達しました: {url}", url=url)

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """pagination_key を辿って data 配列を全件返す。"""
        query = dict(params or {})
        rows: list[dict[str, Any]] = []
        for _ in range(200):  # ページ数の暴走止め
            payload = self._request(path, query)
            batch = payload.get("data", [])
            if isinstance(batch, list):
                rows.extend(batch)
            key = payload.get("pagination_key")
            if not key:
                break
            query["pagination_key"] = key
        return rows

    # -- キャッシュ付き ------------------------------------------------------
    def _cache_path(self, slug: str, day: str) -> Path:
        return self.cfg.cache_dir / slug / day[:4] / f"{day}.json.gz"

    def get_by_date(self, path: str, slug: str, day: str) -> list[dict[str, Any]]:
        """date=<day> のレスポンスをキャッシュ経由で取得する。"""
        cache_file = self._cache_path(slug, day)
        if not self.cfg.no_cache and cache_file.is_file():
            try:
                with gzip.open(cache_file, "rt", encoding="utf-8") as fh:
                    self.cache_hits += 1
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                LOG.debug("キャッシュ破損のため再取得: %s", cache_file)

        rows = self.get(path, {"date": day})
        if not self.cfg.no_cache:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False)
            tmp.replace(cache_file)
        return rows


def _error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("message") or body)
    except ValueError:
        pass
    return (resp.text or "")[:400]


def _explain_status(status: int, body: str, url: str) -> str:
    base = f"HTTP {status} — {url}\n  message: {body}"
    if status in (401, 403):
        return (
            base
            + "\n  認証に失敗しました。JQUANTS_API_KEY が正しいか、"
            + f"プランで当該エンドポイントが使えるか {DASHBOARD_URL} で確認してください。"
        )
    if status == 410:
        return (
            base
            + "\n  410 Gone は API の移行を示します。J-Quants 側の仕様が再度変わった可能性があるため、"
            + f"\n  {MIGRATION_URL} および {SPEC_URL} を確認し、"
            + "\n  screener.py の API_BASE / エンドポイントパス / フィールド名を見直してください。"
        )
    if status == 404:
        return base + f"\n  エンドポイントのパスが変わった可能性があります。{SPEC_URL} を確認してください。"
    return base


# ---------------------------------------------------------------------------
# 評価基準日 (asof) の決定
# ---------------------------------------------------------------------------


def resolve_asof(client: JQuantsClient, cfg: Config) -> str:
    """Free プランで実際にデータが返る最新営業日を探す。

    Free プランは 12 週遅延なので「今日」を指定しても空が返る。
    粗い後ろ向き探索でデータのある日を掴み、そこから前進して最新日を確定する。
    """
    if cfg.asof:
        return cfg.asof

    today = datetime.now().date()
    anchor: Optional[datetime] = None
    for back in (FREE_DELAY_DAYS, FREE_DELAY_DAYS + 7, FREE_DELAY_DAYS + 14,
                 FREE_DELAY_DAYS + 21, FREE_DELAY_DAYS + 35, FREE_DELAY_DAYS + 60):
        day = _prev_weekday(today - timedelta(days=back))
        if client.get_by_date("/equities/bars/daily", "bars_daily", day.isoformat()):
            anchor = datetime.combine(day, datetime.min.time())
            break
    if anchor is None:
        raise JQuantsError(
            "直近約 5 か月のどの日付でも株価データが返りませんでした。"
            f"\n  プランのデータ提供範囲を {SPEC_URL} で確認するか、--asof で基準日を明示してください。"
        )

    latest = anchor.date()
    probe = latest
    for _ in range(20):
        probe = _next_weekday(probe + timedelta(days=1))
        if probe > today:
            break
        if client.get_by_date("/equities/bars/daily", "bars_daily", probe.isoformat()):
            latest = probe
    LOG.info("評価基準日 (asof) = %s  [今日から %d 日前]", latest, (today - latest).days)
    return latest.isoformat()


def _prev_weekday(d):
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _next_weekday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def build_price_dates(asof: str, cfg: Config) -> list[str]:
    """価格を取得する日付リスト。

    直近 dense_days は日次 (流動性・ボラティリティ・直近リターン用)、
    それ以前は weekly_step 間隔でサンプリング (モメンタム用)。
    これで 12 か月分を ~100 リクエストに収める。
    """
    end = datetime.strptime(asof, "%Y-%m-%d").date()
    dense_start = end - timedelta(days=cfg.dense_days)
    hist_start = end - timedelta(days=cfg.hist_days)

    days: set = set()
    d = dense_start
    while d <= end:
        if d.weekday() < 5:
            days.add(d)
        d += timedelta(days=1)

    d = hist_start
    while d < dense_start:
        days.add(_prev_weekday(d))
        d += timedelta(days=cfg.weekly_step)

    return sorted(x.isoformat() for x in days)


def build_fins_dates(asof: str, cfg: Config) -> list[str]:
    """財務サマリを取得する日付リスト (開示は平日のみ)。"""
    end = datetime.strptime(asof, "%Y-%m-%d").date()
    start = end - timedelta(days=cfg.fins_days)
    out: list[str] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------

PRICE_COLS = ["Date", "Code", "C", "AdjC", "Vo", "Va"]
FINS_KEEP = [
    "DiscDate", "DiscTime", "Code", "DocType", "CurPerType", "CurFYSt", "CurFYEn",
    "Sales", "OP", "NP", "EPS", "BPS", "TA", "Eq", "EqAR", "ROE", "CFO",
    "AvgSh", "ShOutFY", "TrShFY",
    "DivAnn", "FDivAnn", "NxFDivAnn",
    "FSales", "FOP", "FNP", "FEPS",
    "NCSales", "NCOP", "NCNP", "NCEPS", "NCBPS", "NCEq", "NCEqAR", "NCROE", "NCTA",
    "FNCSales", "FNCOP", "FNCNP", "FNCEPS",
]


def fetch_frames(client: JQuantsClient, cfg: Config, asof: str) -> dict[str, pd.DataFrame]:
    LOG.info("銘柄マスタを取得中 …")
    master = pd.DataFrame.from_records(client.get("/equities/master", {"date": asof}))
    if master.empty:
        raise JQuantsError(f"銘柄マスタが空です (date={asof})。")

    price_dates = build_price_dates(asof, cfg)
    LOG.info("株価日足を取得中 … %d 営業日分", len(price_dates))
    prices = _fetch_by_dates(client, "/equities/bars/daily", "bars_daily", price_dates, PRICE_COLS)
    if prices.empty:
        raise JQuantsError("株価データが 1 件も取得できませんでした。")

    fins_dates = build_fins_dates(asof, cfg)
    LOG.info("決算サマリを取得中 … %d 営業日分 (初回は時間がかかります)", len(fins_dates))
    fins = _fetch_by_dates(client, "/fins/summary", "fin_summary", fins_dates, None)

    LOG.info(
        "取得完了: master=%d  prices=%d行  fins=%d行  (API %d 回 / キャッシュ %d 回)",
        len(master), len(prices), len(fins), client.request_count, client.cache_hits,
    )
    return {"master": master, "prices": prices, "fins": fins}


def _fetch_by_dates(
    client: JQuantsClient,
    path: str,
    slug: str,
    dates: Sequence[str],
    keep: Optional[list[str]],
) -> pd.DataFrame:
    buff: list[pd.DataFrame] = []
    total = len(dates)
    for i, day in enumerate(dates, 1):
        rows = client.get_by_date(path, slug, day)
        if rows:
            df = pd.DataFrame.from_records(rows)
            if keep:
                for col in keep:
                    if col not in df.columns:
                        df[col] = np.nan
                df = df[keep]
            buff.append(df)
        if i % 50 == 0 or i == total:
            LOG.info("  %s %d/%d", slug, i, total)
    if not buff:
        return pd.DataFrame()
    return pd.concat(buff, ignore_index=True)


# ---------------------------------------------------------------------------
# 汎用ユーティリティ
# ---------------------------------------------------------------------------


def to_num(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        else:
            out[c] = np.nan
    return out


def norm_code(s: pd.Series) -> pd.Series:
    """銘柄コードを 5 桁文字列に揃える (4 桁表記が混ざっても比較できるように)。"""
    v = s.astype(str).str.strip().str.upper()
    return v.where(v.str.len() != 4, v + "0")


def rank_z(s: pd.Series) -> pd.Series:
    """順位ベースの正規化スコア。外れ値に強く、分布の歪みに左右されない。

    Blom 変換 (r - 0.375) / (n + 0.25) を正規分布の逆累積で z 化する。
    """
    r = s.rank(method="average", na_option="keep")
    n = int(r.notna().sum())
    if n < 3:
        return pd.Series(np.nan, index=s.index, dtype=float)
    p = (r - 0.375) / (n + 0.25)
    nd = NormalDist()
    return p.map(lambda x: nd.inv_cdf(float(x)) if pd.notna(x) else np.nan).astype(float)


def group_rank_z(s: pd.Series, groups: Optional[pd.Series], min_group: int) -> pd.Series:
    """業種内で順位 z 化する。構成銘柄が少ない業種は全体で z 化する。

    PBR や PER の水準は業種でまるごと違う (銀行 vs 情報通信)。
    業種内で見ないと、ランキングが毎回同じ数業種の羅列になる。
    """
    if groups is None:
        return rank_z(s)
    out = pd.Series(np.nan, index=s.index, dtype=float)
    overall = rank_z(s)
    for key, idx in groups.groupby(groups, dropna=False).groups.items():
        idx = pd.Index(idx)
        sub = s.loc[idx]
        if int(sub.notna().sum()) >= min_group:
            out.loc[idx] = rank_z(sub)
        else:
            out.loc[idx] = overall.loc[idx]
    missing = out.isna() & overall.notna()
    out.loc[missing] = overall.loc[missing]
    return out


def ensure_columns(df: pd.DataFrame, defaults: dict[str, Any]) -> pd.DataFrame:
    """欠けている列を既定値で埋める。上流のデータが空でも落ちないようにする。"""
    out = df.copy()
    for col, default in defaults.items():
        if col not in out.columns:
            out[col] = default
    return out


def mean_available(df: pd.DataFrame, cols: Sequence[str], min_count: int = 1) -> pd.Series:
    """欠損を無視した平均。非欠損が min_count 未満なら NaN。"""
    present = [c for c in cols if c in df.columns]
    if not present:
        return pd.Series(np.nan, index=df.index, dtype=float)
    sub = df[present]
    out = sub.mean(axis=1, skipna=True)
    return out.where(sub.notna().sum(axis=1) >= min_count)


# ---------------------------------------------------------------------------
# 価格ファクタ
# ---------------------------------------------------------------------------


def compute_price_factors(prices: pd.DataFrame, asof: str, cfg: Config) -> pd.DataFrame:
    px = to_num(prices, ["C", "AdjC", "Vo", "Va"])
    px["Code"] = norm_code(px["Code"])
    px["Date"] = pd.to_datetime(px["Date"], errors="coerce")
    px = px.dropna(subset=["Date", "Code"])
    # AdjC が無い日は C で代用する (調整が入っていない = 係数 1 とみなす)
    px["AdjC"] = px["AdjC"].fillna(px["C"])
    px = px[px["AdjC"] > 0]

    wide_adj = px.pivot_table(index="Date", columns="Code", values="AdjC", aggfunc="last").sort_index()
    wide_c = px.pivot_table(index="Date", columns="Code", values="C", aggfunc="last").sort_index()
    wide_va = px.pivot_table(index="Date", columns="Code", values="Va", aggfunc="last").sort_index()

    asof_ts = pd.Timestamp(asof)
    t = _asof_index(wide_adj.index, asof_ts)
    if t is None:
        raise JQuantsError(f"asof={asof} 以前の株価が panel にありません。")

    def at(offset_days: int) -> pd.Series:
        idx = _asof_index(wide_adj.index, asof_ts - pd.Timedelta(days=offset_days))
        if idx is None:
            return pd.Series(np.nan, index=wide_adj.columns, dtype=float)
        return wide_adj.loc[idx]

    px_t, px_1m, px_6m, px_12m = at(0), at(30), at(182), at(365)

    f = pd.DataFrame(index=wide_adj.columns)
    f.index.name = "Code"
    # 12-1 モメンタム: 直近 1 か月を飛ばす。短期反転の混入を避けるための標準的な定義。
    f["Mom12_1"] = px_1m / px_12m - 1.0
    f["Mom6_1"] = px_1m / px_6m - 1.0
    f["Ret1M"] = px_t / px_1m - 1.0

    hist = wide_adj[wide_adj.index >= asof_ts - pd.Timedelta(days=365)]
    f["Hi52Ratio"] = px_t / hist.max()

    dense = wide_adj[wide_adj.index >= asof_ts - pd.Timedelta(days=cfg.dense_days)]
    rets = np.log(dense / dense.shift(1))
    f["Vol"] = rets.std(skipna=True) * np.sqrt(252.0)

    dense_va = wide_va[wide_va.index >= asof_ts - pd.Timedelta(days=cfg.dense_days)]
    f["Turnover"] = dense_va.median(skipna=True)

    f["Price"] = wide_c.loc[t] if t in wide_c.index else np.nan
    f["PriceDate"] = t
    f["PricePoints"] = wide_adj.notna().sum()
    # 調整係数 k = AdjC / C。1 株あたり指標を株価と比べる際の分割補正に使う。
    f["K_asof"] = (px_t / f["Price"]).replace([np.inf, -np.inf], np.nan)

    k_long = (wide_adj / wide_c).replace([np.inf, -np.inf], np.nan)
    k_long = k_long.stack(future_stack=True).rename("K").reset_index()
    k_long = k_long.dropna(subset=["K"]).sort_values("Date")
    return f, k_long


def _asof_index(index: pd.DatetimeIndex, target: pd.Timestamp):
    """target 以下で最大の日付を返す (無ければ None)。"""
    candidates = index[index <= target]
    return candidates[-1] if len(candidates) else None


# ---------------------------------------------------------------------------
# 財務ファクタ
# ---------------------------------------------------------------------------

FIN_NUM_COLS = [
    "Sales", "OP", "NP", "EPS", "BPS", "TA", "Eq", "EqAR", "ROE", "CFO",
    "AvgSh", "ShOutFY", "TrShFY", "DivAnn", "FDivAnn", "NxFDivAnn",
    "FSales", "FOP", "FNP", "FEPS",
    "NCSales", "NCOP", "NCNP", "NCEPS", "NCBPS", "NCEq", "NCEqAR", "NCROE", "NCTA",
    "FNCSales", "FNCOP", "FNCNP", "FNCEPS",
]


def prepare_fins(fins: pd.DataFrame, asof: Optional[str] = None) -> pd.DataFrame:
    """決算サマリを数値化し、開示時刻順に並べる。

    asof を渡すと、それより後の開示を落とす。キャッシュに新しい日付が
    混ざっていても過去日で再実行できるようにするため (先読み防止)。
    """
    if fins.empty:
        return fins
    df = fins.copy()
    for col in FINS_KEEP:
        if col not in df.columns:
            df[col] = np.nan
    df = df[[c for c in FINS_KEEP if c in df.columns]]
    df = to_num(df, FIN_NUM_COLS)
    df["Code"] = norm_code(df["Code"])
    df["DiscDate"] = pd.to_datetime(df["DiscDate"], errors="coerce")
    for c in ("CurFYSt", "CurFYEn"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    df["CurPerType"] = df["CurPerType"].astype(str).str.strip().str.upper()
    df["DiscTime"] = df["DiscTime"].astype(str).fillna("")
    df = df.dropna(subset=["Code", "DiscDate"])
    if asof:
        df = df[df["DiscDate"] <= pd.Timestamp(asof)]
    return df.sort_values(["Code", "DiscDate", "DiscTime"]).reset_index(drop=True)


def _coalesce(*series: pd.Series) -> pd.Series:
    """先頭から順に、欠損でない値を採用する。"""
    out = series[0].copy()
    for s in series[1:]:
        out = out.where(out.notna(), s)
    return out


def extract_financials(fins: pd.DataFrame) -> pd.DataFrame:
    """銘柄ごとに最新開示 + 直近本決算からファンダメンタルズを組み立てる。

    連結 (Sales/OP/...) が欠けている場合は非連結 (NC*) で補完する。
    新興・小型では非連結しか出さない会社があるため。
    """
    if fins.empty:
        return pd.DataFrame(columns=["Code"]).set_index("Code")

    latest = fins.groupby("Code", as_index=False).tail(1).set_index("Code")
    fy_rows = fins[fins["CurPerType"] == "FY"]
    latest_fy = fy_rows.groupby("Code", as_index=False).tail(1).set_index("Code")

    idx = latest.index
    out = pd.DataFrame(index=idx)
    out.index.name = "Code"
    out["DiscDate"] = latest["DiscDate"]
    out["CurPerType"] = latest["CurPerType"]
    out["CurFYEn"] = latest["CurFYEn"]

    # --- ストック指標は最新開示の貸借対照表から -----------------------------
    out["BPS"] = _coalesce(latest["BPS"], latest["NCBPS"])
    out["EqRatio"] = _coalesce(latest["EqAR"], latest["NCEqAR"])
    out["ROE"] = _coalesce(latest["ROE"], latest["NCROE"])
    out["Equity"] = _coalesce(latest["Eq"], latest["NCEq"])
    out["TotalAssets"] = _coalesce(latest["TA"], latest["NCTA"])

    # --- 利益は「今期会社予想 -> 直近本決算実績」の順で採用 -----------------
    eps_fc = _coalesce(latest["FEPS"], latest["FNCEPS"])
    eps_fy = _coalesce(
        latest_fy["EPS"].reindex(idx), latest_fy["NCEPS"].reindex(idx)
    )
    out["EPS_Forecast"] = eps_fc
    out["EPS_ActualFY"] = eps_fy
    out["EPS"] = eps_fc.where(eps_fc > 0, eps_fy)
    out["EPSBasis"] = np.where(
        (eps_fc > 0) & eps_fc.notna(), "会社予想",
        np.where(eps_fy.notna(), "前期実績", "不明"),
    )

    sales = _coalesce(latest_fy["Sales"].reindex(idx), latest_fy["NCSales"].reindex(idx))
    op = _coalesce(latest_fy["OP"].reindex(idx), latest_fy["NCOP"].reindex(idx))
    np_ = _coalesce(latest_fy["NP"].reindex(idx), latest_fy["NCNP"].reindex(idx))
    out["Sales"] = sales
    out["OP"] = op
    out["NP"] = np_
    out["OPMargin"] = np.where((sales > 0) & op.notna(), op / sales * 100.0, np.nan)

    cfo = latest_fy["CFO"].reindex(idx)
    out["CFO"] = cfo
    out["AvgShares"] = _coalesce(latest_fy["AvgSh"].reindex(idx), latest["AvgSh"])
    out["CFOPS"] = np.where(out["AvgShares"] > 0, cfo / out["AvgShares"], np.nan)
    # アクルーアル: 利益とキャッシュの乖離。大きいほど利益の質が低い。
    out["Accruals"] = np.where(
        (out["TotalAssets"] > 0) & np_.notna() & cfo.notna(),
        (np_ - cfo) / out["TotalAssets"], np.nan,
    )
    return out


def dividend_signals(fins: pd.DataFrame) -> pd.DataFrame:
    """増配シグナルを 2 種類作る。

    DivIncrease  : 今期予想の年間配当 > 前期実績の年間配当
    DivRevisedUp : 同一決算期の予想配当が期中に引き上げられた (増配修正)
    OPRevision   : 同一決算期の営業利益予想の変化率 (業績修正)
    """
    cols = ["DivForecast", "DivPrevActual", "DivIncrease", "DivIncreasePct",
            "DivRevisedUp", "DivYieldSrc", "OPRevision"]
    if fins.empty:
        return pd.DataFrame(columns=cols)

    recs: list[dict[str, Any]] = []
    for code, g in fins.groupby("Code", sort=False):
        last = g.iloc[-1]
        is_fy = last["CurPerType"] == "FY"
        if is_fy:
            # 本決算開示: 実績 = 当期 DivAnn、予想 = 翌期 NxFDivAnn
            div_fc = last.get("NxFDivAnn", np.nan)
            div_prev = last.get("DivAnn", np.nan)
        else:
            div_fc = last.get("FDivAnn", np.nan)
            prev_fy = g[(g["CurPerType"] == "FY") & (g["CurFYEn"] < last["CurFYSt"])]
            div_prev = prev_fy.iloc[-1]["DivAnn"] if len(prev_fy) else np.nan
            if pd.isna(div_prev):
                # 前期の本決算を取得期間内に拾えなかった場合の保険
                div_prev = last.get("DivAnn", np.nan)

        same_fy = g[g["CurFYEn"] == last["CurFYEn"]]
        fc_series = same_fy["NxFDivAnn" if is_fy else "FDivAnn"].dropna()
        div_up = bool(len(fc_series) >= 2 and fc_series.iloc[-1] > fc_series.iloc[0])

        op_series = same_fy["FOP"].dropna()
        op_rev = float(op_series.iloc[-1] / op_series.iloc[0] - 1.0) if (
            len(op_series) >= 2 and op_series.iloc[0] > 0
        ) else np.nan

        inc = bool(pd.notna(div_fc) and pd.notna(div_prev) and div_fc > div_prev)
        pct = float(div_fc / div_prev - 1.0) if (
            pd.notna(div_fc) and pd.notna(div_prev) and div_prev > 0
        ) else np.nan

        recs.append({
            "Code": code,
            "DivForecast": div_fc, "DivPrevActual": div_prev,
            "DivIncrease": inc, "DivIncreasePct": pct,
            "DivRevisedUp": div_up,
            "DivYieldSrc": "翌期予想" if is_fy else "今期予想",
            "OPRevision": op_rev,
        })
    return pd.DataFrame.from_records(recs).set_index("Code")


# ---------------------------------------------------------------------------
# 組み立て・スコアリング
# ---------------------------------------------------------------------------

MASTER_KEEP = ["Code", "CoName", "S17", "S17Nm", "S33", "S33Nm", "ScaleCat", "Mkt", "MktNm"]


def prepare_master(master: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    df = master.copy()
    for c in MASTER_KEEP:
        if c not in df.columns:
            df[c] = np.nan
    df["Code"] = norm_code(df["Code"])
    if "Date" in master.columns:
        df["Date"] = pd.to_datetime(master["Date"], errors="coerce")
        df = df.sort_values("Date").groupby("Code", as_index=False).tail(1)
    df = df[MASTER_KEEP].drop_duplicates(subset=["Code"]).set_index("Code")
    df["Mkt"] = df["Mkt"].astype(str).str.strip()
    if cfg.markets:
        df = df[df["Mkt"].isin(cfg.markets)]
    # 業種未分類 (ETF/REIT/その他) は個別株スクリーニングの対象外
    df = df[~df["S33"].astype(str).str.strip().isin({"9999", "nan", ""})]
    return df


def apply_gates(t: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """ハードゲート。落とした理由を必ず残し、後から監査できるようにする。"""
    reasons: list[list[str]] = [[] for _ in range(len(t))]

    def flag(mask: pd.Series, text: str) -> None:
        for pos in np.flatnonzero(mask.fillna(True).to_numpy()):
            reasons[pos].append(text)

    flag(t["PricePoints"] < cfg.min_price_points, "価格データ不足")
    flag(t["Price"].isna() | (t["Price"] <= 0), "株価取得不可")
    flag(t["Turnover"] < cfg.min_turnover, f"流動性不足(<{cfg.min_turnover:,.0f}円/日)")
    flag(t["BPS"].isna() & t["EPS"].isna(), "財務データなし")
    flag(t["BPS"] <= 0, "純資産マイナス")
    flag(t["EqRatio"] < cfg.min_equity_ratio, f"自己資本比率<{cfg.min_equity_ratio:g}%")
    flag(t["EPS"] <= 0, "予想・実績とも赤字")
    flag(t["Mom12_1"].isna(), "モメンタム算出不可")

    t = t.copy()
    t["ExcludeReason"] = [";".join(r) for r in reasons]
    t["PassedGates"] = t["ExcludeReason"] == ""
    return t


def apply_momentum_filter(t: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """モメンタムの両端を落とす。

    旧版の「固定シグマで外れ値除外」だと、相場全体が動いた月に大量除外/ゼロ除外の
    どちらかに振れる。クロスセクション百分位なら毎回おおよそ一定数だけ落ちる。

    下側 : 下落トレンドの継続。割安に見える理由が「業績がまだ落ちている」ケース。
    上側 : 急騰済み。指値が刺さらず、レポートに載せても実行できない。
    """
    t = t.copy()
    t["MomPct"] = np.nan
    t["ExcludedByMomentum"] = False
    t["MomentumNote"] = ""

    pool = t["PassedGates"] & t["Mom12_1"].notna()
    if pool.sum() >= 20:
        pct = t.loc[pool, "Mom12_1"].rank(pct=True) * 100.0
        t.loc[pool, "MomPct"] = pct
        low = pool & (t["MomPct"] < cfg.mom_low_pct)
        high = pool & (t["MomPct"] > cfg.mom_high_pct)
        t.loc[low, "ExcludedByMomentum"] = True
        t.loc[low, "MomentumNote"] = f"下位{cfg.mom_low_pct:g}%(下落継続)"
        t.loc[high, "ExcludedByMomentum"] = True
        t.loc[high, "MomentumNote"] = f"上位{100 - cfg.mom_high_pct:g}%(急騰)"
    return t


def score(t: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """業種内順位 z を合成して最終スコアを作る。"""
    t = t.copy()
    pool = t["PassedGates"]
    sub = t[pool]
    groups = sub["S33Nm"] if cfg.sector_neutral else None

    def z(col: str, sign: float = 1.0) -> pd.Series:
        if col not in sub.columns:
            return pd.Series(np.nan, index=sub.index, dtype=float)
        return group_rank_z(sub[col] * sign, groups, cfg.min_sector_size)

    zs = pd.DataFrame(index=sub.index)
    zs["z_EarnYield"] = z("EarnYield")
    zs["z_BookYield"] = z("BookYield")
    zs["z_CFOYield"] = z("CFOYield")
    zs["z_DivYield"] = z("DivYield")
    zs["z_ROE"] = z("ROE")
    zs["z_EqRatio"] = z("EqRatio")
    zs["z_OPMargin"] = z("OPMargin")
    zs["z_Accruals"] = z("Accruals", -1.0)      # 低いほど良い
    zs["z_Mom"] = z("Mom12_1")
    zs["z_OPRevision"] = z("OPRevision")
    zs["z_LowVol"] = z("Vol", -1.0)             # 低いほど良い

    zs["ValueZ"] = mean_available(zs, ["z_EarnYield", "z_BookYield", "z_CFOYield", "z_DivYield"], 2)
    zs["QualityZ"] = mean_available(zs, ["z_ROE", "z_EqRatio", "z_OPMargin", "z_Accruals"], 2)
    zs["MomentumZ"] = mean_available(zs, ["z_Mom", "z_OPRevision"], 1)
    zs["LowRiskZ"] = zs["z_LowVol"]

    zs["Score"] = (
        cfg.w_value * zs["ValueZ"].fillna(0.0)
        + cfg.w_quality * zs["QualityZ"].fillna(0.0)
        + cfg.w_momentum * zs["MomentumZ"].fillna(0.0)
        + cfg.w_lowrisk * zs["LowRiskZ"].fillna(0.0)
    )
    # バリューが計算できない銘柄は「割安スクリーナー」の対象にしない
    zs.loc[zs["ValueZ"].isna(), "Score"] = np.nan

    for c in zs.columns:
        if c not in t.columns:
            t[c] = np.nan
        t.loc[sub.index, c] = zs[c]

    eligible = t["PassedGates"] & ~t["ExcludedByMomentum"] & t["Score"].notna()
    t["Eligible"] = eligible
    t["Rank"] = np.nan
    t.loc[eligible, "Rank"] = t.loc[eligible, "Score"].rank(ascending=False, method="first")
    return t.sort_values(["Rank", "Score"], ascending=[True, False], na_position="last")


def build_table(
    master: pd.DataFrame,
    price_factors: pd.DataFrame,
    k_long: pd.DataFrame,
    fin: pd.DataFrame,
    div: pd.DataFrame,
    cfg: Config,
) -> pd.DataFrame:
    t = prepare_master(master, cfg).join(price_factors, how="inner")
    t = t.join(fin, how="left").join(div, how="left")
    t = ensure_columns(t, {
        "DiscDate": pd.NaT, "CurPerType": np.nan, "CurFYEn": pd.NaT,
        "BPS": np.nan, "EqRatio": np.nan, "ROE": np.nan, "Equity": np.nan,
        "TotalAssets": np.nan, "EPS": np.nan, "EPS_Forecast": np.nan,
        "EPS_ActualFY": np.nan, "EPSBasis": "不明", "Sales": np.nan, "OP": np.nan,
        "NP": np.nan, "OPMargin": np.nan, "CFO": np.nan, "AvgShares": np.nan,
        "CFOPS": np.nan, "Accruals": np.nan,
        "DivForecast": np.nan, "DivPrevActual": np.nan, "DivIncrease": False,
        "DivIncreasePct": np.nan, "DivRevisedUp": False, "DivYieldSrc": np.nan,
        "OPRevision": np.nan,
    })

    # --- 1 株あたり指標の分割補正 ------------------------------------------
    t["K_disc"] = np.nan
    if not k_long.empty and "DiscDate" in t.columns and t["DiscDate"].notna().any():
        left = (
            t.reset_index()[["Code", "DiscDate"]]
            .dropna(subset=["DiscDate"])
            .sort_values("DiscDate")
        )
        right = k_long.sort_values("Date").copy()
        # merge_asof は結合キーの datetime 解像度が一致していないと落ちる。
        # 由来 (API のレスポンス / pivot の index) で s と us が混ざるので揃える。
        left["DiscDate"] = left["DiscDate"].astype("datetime64[ns]")
        right["Date"] = right["Date"].astype("datetime64[ns]")
        merged = pd.merge_asof(
            left, right,
            left_on="DiscDate", right_on="Date", by="Code", direction="backward",
        ).set_index("Code")
        t.loc[merged.index, "K_disc"] = merged["K"]
    adj = (t["K_disc"] / t["K_asof"]).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    t["SplitAdj"] = adj

    price = t["Price"]
    t["EarnYield"] = t["EPS"] * adj / price
    t["BookYield"] = t["BPS"] * adj / price
    t["CFOYield"] = t["CFOPS"] * adj / price
    t["DivYield"] = t["DivForecast"] * adj / price
    # 逆数も出しておく (人が読むのは PER/PBR のほう)
    t["PER"] = np.where(t["EarnYield"] > 0, 1.0 / t["EarnYield"], np.nan)
    t["PBR"] = np.where(t["BookYield"] > 0, 1.0 / t["BookYield"], np.nan)
    for c in ("DivIncrease", "DivRevisedUp"):
        t[c] = t[c].fillna(False).astype(bool)

    t = apply_gates(t, cfg)
    t = apply_momentum_filter(t, cfg)
    t = score(t, cfg)
    return t


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

OUT_COLS = [
    "CoName", "MktNm", "S33Nm", "ScaleCat", "Rank", "Score",
    "ValueZ", "QualityZ", "MomentumZ", "LowRiskZ",
    "Price", "PER", "PBR", "EarnYield", "BookYield", "CFOYield", "DivYield",
    "EPS", "EPSBasis", "BPS", "ROE", "EqRatio", "OPMargin", "Accruals",
    "Mom12_1", "Mom6_1", "Ret1M", "MomPct", "Hi52Ratio", "Vol", "Turnover",
    "DivForecast", "DivPrevActual", "DivIncrease", "DivIncreasePct", "DivRevisedUp",
    "OPRevision", "DiscDate", "SplitAdj",
    "PassedGates", "Eligible", "ExcludedByMomentum", "MomentumNote", "ExcludeReason",
]


def _fmt(v: Any, kind: str = "num") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return "–"
    if kind == "pct":
        return f"{v * 100:,.1f}%"
    if kind == "yen":
        return f"{v:,.0f}"
    if kind == "x":
        return f"{v:,.1f}x"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def write_outputs(t: pd.DataFrame, cfg: Config, asof: str, stats: dict[str, Any]) -> dict[str, Path]:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    tag = asof.replace("-", "")
    cols = [c for c in OUT_COLS if c in t.columns]

    full_path = cfg.out_dir / f"screen_{tag}.csv"
    t[cols].to_csv(full_path, encoding="utf-8-sig")

    top = t[t["Eligible"]].head(cfg.top_n)
    top_path = cfg.out_dir / f"screen_{tag}_top.csv"
    top[cols].to_csv(top_path, encoding="utf-8-sig")

    report_path = cfg.out_dir / f"report_{tag}.md"
    report_path.write_text(render_report(t, cfg, asof, stats), encoding="utf-8")
    return {"full": full_path, "top": top_path, "report": report_path}


def render_report(t: pd.DataFrame, cfg: Config, asof: str, stats: dict[str, Any]) -> str:
    eligible = t[t["Eligible"]]
    top = eligible.head(cfg.top_n)
    mom_excluded = t[t["ExcludedByMomentum"]]
    div_up = eligible[eligible["DivIncrease"]]
    div_rev = eligible[eligible["DivRevisedUp"]]

    L: list[str] = []
    A = L.append
    A(f"# スクリーニングレポート {asof}")
    A("")
    A(f"- 実行日時: {datetime.now():%Y-%m-%d %H:%M:%S}")
    A(f"- 評価基準日 (asof): **{asof}** — Free プランは約 12 週遅延のため、"
      f"実行日より {stats.get('lag_days', '?')} 日前のデータです")
    A(f"- 対象市場: {', '.join(MARKET_NAMES.get(m, m) for m in cfg.markets)}")
    A(f"- スコア重み: バリュー {cfg.w_value} / クオリティ {cfg.w_quality} / "
      f"モメンタム {cfg.w_momentum} / 低リスク {cfg.w_lowrisk}"
      f"{' / 業種内で正規化' if cfg.sector_neutral else ' / 全体で正規化'}")
    A("")
    A("## 1. サマリ")
    A("")
    A("| 項目 | 銘柄数 |")
    A("|---|---:|")
    A(f"| ユニバース (対象市場の個別株) | {len(t):,} |")
    A(f"| ハードゲート通過 | {int(t['PassedGates'].sum()):,} |")
    A(f"| モメンタム除外 (ExcludedByMomentum) | {len(mom_excluded):,} |")
    A(f"| **最終候補** | **{len(eligible):,}** |")
    A(f"| うち増配 (今期予想 > 前期実績) | {len(div_up):,} |")
    A(f"| うち期中の増配修正 | {len(div_rev):,} |")
    A("")

    if not t["ExcludeReason"].eq("").all():
        A("### ゲート除外の内訳")
        A("")
        counts: dict[str, int] = {}
        for row in t.loc[t["ExcludeReason"] != "", "ExcludeReason"]:
            for r in row.split(";"):
                counts[r] = counts.get(r, 0) + 1
        A("| 理由 | 件数 |")
        A("|---|---:|")
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            A(f"| {reason} | {n:,} |")
        A("")

    A(f"## 2. 上位 {min(cfg.top_n, len(top))} 銘柄")
    A("")
    if top.empty:
        A("該当なし。ゲートが厳しすぎる可能性があります "
          "(`--min-turnover` を下げる、`--quick` を外して財務取得期間を伸ばす等をお試しください)。")
    else:
        A("| # | コード | 銘柄名 | 業種 | 株価 | PER | PBR | 配当利回 | ROE | 自己資本比率 | "
          "12-1モメンタム | Score | 増配 |")
        A("|--:|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|:-:|")
        for i, (code, r) in enumerate(top.iterrows(), 1):
            marks = ("↑" if r["DivIncrease"] else "") + ("★" if r["DivRevisedUp"] else "")
            A(
                f"| {i} | {code} | {r['CoName']} | {r['S33Nm']} | "
                f"{_fmt(r['Price'], 'yen')} | {_fmt(r['PER'], 'x')} | {_fmt(r['PBR'], 'x')} | "
                f"{_fmt(r['DivYield'], 'pct')} | {_fmt(r['ROE'])}% | {_fmt(r['EqRatio'])}% | "
                f"{_fmt(r['Mom12_1'], 'pct')} | {_fmt(r['Score'])} | {marks or '–'} |"
            )
        A("")
        A("↑ = 今期予想年間配当が前期実績を上回る / ★ = 期中に配当予想を引き上げ")
    A("")

    A("## 3. モメンタム除外")
    A("")
    if mom_excluded.empty:
        A("除外なし。")
    else:
        low = mom_excluded[mom_excluded["MomentumNote"].str.contains("下落継続", na=False)]
        high = mom_excluded[mom_excluded["MomentumNote"].str.contains("急騰", na=False)]
        A(f"合計 {len(mom_excluded):,} 銘柄 (下落継続 {len(low):,} / 急騰 {len(high):,})。")
        A("")
        A("バリュー指標だけで並べると上位に来るが、12-1 モメンタムが両端にあるため外した銘柄:")
        A("")
        A("| コード | 銘柄名 | PER | PBR | 12-1モメンタム | 除外理由 |")
        A("|---|---|--:|--:|--:|---|")
        shown = mom_excluded.sort_values("ValueZ", ascending=False).head(15)
        for code, r in shown.iterrows():
            A(f"| {code} | {r['CoName']} | {_fmt(r['PER'], 'x')} | {_fmt(r['PBR'], 'x')} | "
              f"{_fmt(r['Mom12_1'], 'pct')} | {r['MomentumNote']} |")
    A("")

    A("## 4. 増配銘柄 (最終候補のうち)")
    A("")
    if div_up.empty and div_rev.empty:
        A("該当なし。"
          + ("\n\n`--quick` 実行では前期本決算を取得範囲に含めないため、"
             "「今期予想 > 前期実績」の判定ができません。通常実行をお試しください。"
             if cfg.fins_days < 400 else ""))
    else:
        merged = pd.concat([div_up, div_rev]).drop_duplicates()
        merged = merged.sort_values("DivYield", ascending=False)
        A("| コード | 銘柄名 | 前期実績 | 今期予想 | 増配率 | 配当利回 | 期中増額 |")
        A("|---|---|--:|--:|--:|--:|:-:|")
        for code, r in merged.head(25).iterrows():
            A(f"| {code} | {r['CoName']} | {_fmt(r['DivPrevActual'], 'yen')} | "
              f"{_fmt(r['DivForecast'], 'yen')} | {_fmt(r['DivIncreasePct'], 'pct')} | "
              f"{_fmt(r['DivYield'], 'pct')} | {'★' if r['DivRevisedUp'] else '–'} |")
    A("")

    if not eligible.empty:
        A("## 5. 最終候補の業種分布")
        A("")
        A("| 業種 | 候補数 |")
        A("|---|---:|")
        for sector, n in eligible["S33Nm"].value_counts().head(15).items():
            A(f"| {sector} | {n} |")
        A("")

    A("## 6. 読むときの注意")
    A("")
    A(f"- **データが古い**: Free プランは 12 週遅延。上の株価・指標はすべて {asof} 時点で、"
      "現在値ではありません。発注判断にはそのまま使えません。")
    A("- **利益は会社予想を優先**: PER の分母は今期会社予想 EPS (取れない場合は前期実績)。"
      "`EPSBasis` 列でどちらを使ったか確認できます。")
    A("- **TTM ではない**: 四半期を跨いだ実績 12 か月利益の合成は行っていません。"
      "期ズレのある会社は PER が実態とずれます。")
    A("- **時価総額を使っていない**: 発行済株式数の期ズレを避けるため、"
      "評価はすべて 1 株あたり指標 x 分割調整係数で行っています。")
    A("- スコアは相対順位です。市場全体が割高でも上位 30 銘柄は必ず出ます。")
    A("")
    A(f"- 仕様: {SPEC_URL} / 移行ガイド: {MIGRATION_URL}")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# 自己検証 (ネットワーク不要)
# ---------------------------------------------------------------------------


def _synthetic_dataset(asof: str, n_codes: int = 240) -> dict[str, pd.DataFrame]:
    """既知の仕掛けを埋め込んだ合成データ。ロジックの回帰テストに使う。

    仕掛け:
      10000 : 2:1 分割あり。10006 と調整後株価・1 株利益が完全に一致するよう
              作ってあるので、分割補正が効いていれば両者の PER は一致する。
      10001 : 増配 (前期実績 20 円 -> 今期予想 30 円)
      10002 : 期中の増配修正 (25 円 -> 30 円)
      10003 : 流動性不足
      10004 : 純資産マイナス
      10005 : 予想・実績とも赤字
      10006 : 10000 の対照銘柄 (分割なし)
    """
    n_codes = max(n_codes, 7)  # 対照銘柄 10006 まで必ず作る
    rng = np.random.default_rng(20260826)
    cfg = Config()
    T = pd.Timestamp(asof)
    dates = pd.to_datetime(build_price_dates(asof, cfg))
    codes = [f"{10000 + i}" for i in range(n_codes)]

    # 決算カレンダー: 前期末 T-320、今期は T+45 まで。Q3 開示 (T-5) が最新。
    prev_fy_en = T - pd.Timedelta(days=320)
    cur_fy_st = prev_fy_en + pd.Timedelta(days=1)
    cur_fy_en = T + pd.Timedelta(days=45)
    prev_fy_st = prev_fy_en - pd.Timedelta(days=364)
    def _wd(ts: pd.Timestamp) -> pd.Timestamp:
        return pd.Timestamp(_prev_weekday(ts.date()))          # 開示は営業日のみ

    disc_prev_fy = _wd(prev_fy_en + pd.Timedelta(days=45))     # T-275 前後
    disc_q = [_wd(T - pd.Timedelta(days=185)), _wd(T - pd.Timedelta(days=95)),
              _wd(T - pd.Timedelta(days=5))]                   # Q1 / Q2 / Q3
    split_date = T - pd.Timedelta(days=120)                    # 10000 の分割効力発生日

    # --- 銘柄ごとのパラメータ ---------------------------------------------
    params: dict[str, dict[str, Any]] = {}
    for i, code in enumerate(codes):
        adj = 1000.0 * np.exp(np.cumsum(
            rng.normal(0.0, 0.0016) + rng.normal(0.0, 0.018, len(dates))
        ))
        sales = float(rng.uniform(2e10, 9e11))
        op = sales * float(rng.uniform(0.02, 0.18))
        params[code] = {
            "adj": adj,
            "turnover": 5_000.0 if code == "10003" else float(rng.uniform(5e7, 5e9)),
            "bps": -100.0 if code == "10004" else float(rng.uniform(400, 2500)),
            "eps_fc": -10.0 if code == "10005" else float(rng.uniform(20, 220)),
            "eps_fy_mult": float(rng.uniform(0.7, 1.2)),
            "sales": sales, "op": op, "npf": op * 0.68,
            "cfo": op * 0.68 * float(rng.uniform(0.6, 1.6)),
            "eqar": 12.0 if code == "10004" else float(rng.uniform(25, 78)),
            "roe": float(rng.uniform(2, 22)),
            "split": False,
            "quarters": 3,
        }
    # 10000 は 10006 の完全なクローン + 2:1 分割。最新開示は分割前の Q1 のみ。
    params["10000"] = dict(params["10006"], split=True, quarters=1)

    # --- 株価 --------------------------------------------------------------
    rows = []
    for code, p in params.items():
        adj = p["adj"]
        close = adj * 2.0 if p["split"] else adj.copy()
        if p["split"]:
            close = np.where(dates < split_date, adj * 2.0, adj)
        for d, a, c in zip(dates, adj, close):
            rows.append({"Date": d, "Code": code, "C": float(c), "AdjC": float(a),
                         "Vo": p["turnover"] / max(float(c), 1.0), "Va": p["turnover"]})
    prices = pd.DataFrame(rows)

    master = pd.DataFrame({
        "Date": T, "Code": codes,
        "CoName": [f"テスト{c}" for c in codes],
        "S17": [f"{i % 9 + 1}" for i in range(n_codes)],
        "S17Nm": [f"大分類{i % 9}" for i in range(n_codes)],
        "S33": [f"{3050 + i % 12}" for i in range(n_codes)],
        "S33Nm": [f"業種{i % 12}" for i in range(n_codes)],
        "ScaleCat": "TOPIX Mid400",
        "Mkt": "0111", "MktNm": "プライム",
    })

    # --- 決算 --------------------------------------------------------------
    fins_rows = []
    for code, p in params.items():
        # 分割前の開示なので 1 株あたり指標は分割前ベース (= 2 倍) で出す
        ps = 2.0 if p["split"] else 1.0
        eps_fc = p["eps_fc"] * ps
        eps_fy = p["eps_fc"] * p["eps_fy_mult"] * ps
        bps = p["bps"] * ps
        sales, op, npf, cfo = p["sales"], p["op"], p["npf"], p["cfo"]
        base = {
            "Code": code, "DocType": "FYFinancialStatements_Consolidated_JP",
            "DiscTime": "15:00", "BPS": bps, "EqAR": p["eqar"], "ROE": p["roe"],
            "Eq": 1e11, "TA": 3e11, "AvgSh": 1e8 / ps, "ShOutFY": 1.05e8, "TrShFY": 5e6,
        }
        fins_rows.append({
            **base, "DiscDate": disc_prev_fy, "CurPerType": "FY",
            "CurFYSt": prev_fy_st, "CurFYEn": prev_fy_en,
            "Sales": sales * 0.95, "OP": op * 0.9, "NP": npf * 0.9,
            "EPS": eps_fy, "CFO": cfo * 0.9,
            "DivAnn": 20.0, "FDivAnn": 20.0, "NxFDivAnn": 20.0,
            "FSales": sales, "FOP": op, "FNP": npf, "FEPS": eps_fc,
        })
        div_by_q = {
            "10001": [30.0, 30.0, 30.0],
            "10002": [25.0, 30.0, 30.0],
        }.get(code, [20.0, 20.0, 20.0])
        for q in range(p["quarters"]):
            fins_rows.append({
                **base, "DiscDate": disc_q[q], "CurPerType": f"{q + 1}Q",
                "CurFYSt": cur_fy_st, "CurFYEn": cur_fy_en,
                "Sales": sales * 0.25 * (q + 1), "OP": op * 0.25 * (q + 1),
                "NP": npf * 0.25 * (q + 1), "EPS": eps_fc * 0.25 * (q + 1),
                "CFO": cfo * 0.25 * (q + 1),
                "DivAnn": np.nan, "FDivAnn": div_by_q[q], "NxFDivAnn": np.nan,
                "FSales": sales * 1.05, "FOP": op * (1.0 + 0.03 * q),
                "FNP": npf * 1.05, "FEPS": eps_fc,
            })
    return {"master": master, "prices": prices, "fins": pd.DataFrame(fins_rows)}


def self_test() -> int:
    logging.getLogger().setLevel(logging.INFO)
    asof = (datetime.now().date() - timedelta(days=FREE_DELAY_DAYS)).isoformat()
    asof = _prev_weekday(datetime.strptime(asof, "%Y-%m-%d").date()).isoformat()
    LOG.info("自己検証を開始 (asof=%s, ネットワーク不要)", asof)

    cfg = Config(api_key="dummy", min_turnover=3e7)
    data = _synthetic_dataset(asof)
    pf, k_long = compute_price_factors(data["prices"], asof, cfg)
    fins = prepare_fins(data["fins"], asof)
    fin = extract_financials(fins)
    div = dividend_signals(fins)
    t = build_table(data["master"], pf, k_long, fin, div, cfg)

    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        LOG.info("  [%s] %s", "OK " if cond else "NG ", label)
        if not cond:
            failures.append(label)

    # 取得ウィンドウ (平日のみ) が仕掛けの開示日を実際に拾えるかを先に確かめる。
    # ここを通さないと、日付が週末に落ちて増配判定が消えても気付けない。
    fetch_days = set(build_fins_dates(asof, cfg))
    disc_days = set(data["fins"]["DiscDate"].dt.strftime("%Y-%m-%d"))
    check(disc_days <= fetch_days,
          f"開示日が取得ウィンドウに全て含まれる (漏れ {sorted(disc_days - fetch_days)})")
    price_days = set(build_price_dates(asof, cfg))
    check(len(price_days) < 130, f"株価リクエストが 130 本未満に収まる ({len(price_days)})")

    check(len(t) == 240, f"ユニバース 240 銘柄 (実際 {len(t)})")
    check(t["Eligible"].sum() > 100, f"最終候補が十分にある ({int(t['Eligible'].sum())})")

    adj = t.loc["10000", "SplitAdj"]
    check(abs(adj - 0.5) < 0.02, f"10000 の分割調整係数 = 0.5 (実際 {adj:.3f})")
    # 10000 (分割あり) と 10006 (対照・分割なし) は実質同一銘柄。
    # 分割補正が効いていれば PER / PBR が一致する。
    for metric in ("PER", "PBR"):
        a, b = t.loc["10000", metric], t.loc["10006", metric]
        check(pd.notna(a) and pd.notna(b) and abs(a / b - 1.0) < 0.01,
              f"分割銘柄 10000 と対照 10006 の {metric} が一致 ({a:.2f} vs {b:.2f})")
    check(abs(t.loc["10006", "SplitAdj"] - 1.0) < 1e-9, "分割のない 10006 は補正係数 1.0")

    check(bool(t.loc["10001", "DivIncrease"]), "10001 が増配と判定される")
    check(abs(t.loc["10001", "DivIncreasePct"] - 0.5) < 1e-6,
          f"10001 の増配率 = 50% (実際 {t.loc['10001', 'DivIncreasePct']:.3f})")
    check(bool(t.loc["10002", "DivRevisedUp"]), "10002 が期中の増配修正と判定される")
    check(not bool(t.loc["10003", "PassedGates"])
          and "流動性不足" in t.loc["10003", "ExcludeReason"], "10003 が流動性で除外される")
    check("純資産マイナス" in t.loc["10004", "ExcludeReason"], "10004 が純資産マイナスで除外される")
    check("赤字" in t.loc["10005", "ExcludeReason"], "10005 が赤字で除外される")

    passed = int(t["PassedGates"].sum())
    excl = int(t["ExcludedByMomentum"].sum())
    expect = passed * (cfg.mom_low_pct + (100 - cfg.mom_high_pct)) / 100.0
    check(abs(excl - expect) <= max(3, expect * 0.35),
          f"モメンタム除外が想定件数付近 ({excl} vs 期待 {expect:.0f})")

    ranked = t[t["Eligible"]]
    check(ranked["Score"].is_monotonic_decreasing, "Score の降順にランクが並ぶ")
    check(not ranked["ExcludedByMomentum"].any(), "最終候補にモメンタム除外銘柄が混ざらない")

    sector_counts = ranked.head(30)["S33Nm"].nunique()
    check(sector_counts >= 5, f"上位30の業種が分散している ({sector_counts} 業種)")

    text = render_report(t, cfg, asof, {"lag_days": FREE_DELAY_DAYS})
    check(len(text) > 1500 and "## 3. モメンタム除外" in text, "レポートが生成される")

    if failures:
        LOG.error("自己検証 NG: %d 件", len(failures))
        for f in failures:
            LOG.error("  - %s", f)
        return 1
    LOG.info("自己検証 すべて OK")
    return 0


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="J-Quants API v2 (Free プラン) バリュー x クオリティ スクリーナー",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--asof", help="評価基準日 (YYYY-MM-DD)。既定は取得可能な最新営業日を自動検出")
    p.add_argument("--out", help="出力ディレクトリ (既定: 環境変数 SCREENER_OUT または ./out)")
    p.add_argument("--cache", help="キャッシュディレクトリ (既定: ./cache)")
    p.add_argument("--markets", help="対象市場コードをカンマ区切りで (既定: 0111,0112,0113)")
    p.add_argument("--top-n", type=int, help="レポートに載せる上位件数")
    p.add_argument("--min-turnover", type=float, help="売買代金中央値の下限 (円/日)")
    p.add_argument("--min-equity-ratio", type=float, help="自己資本比率の下限 (%%)")
    p.add_argument("--mom-low-pct", type=float,
                   help="この百分位未満のモメンタムを除外 (下落継続)")
    p.add_argument("--mom-high-pct", type=float,
                   help="この百分位超のモメンタムを除外 (急騰)")
    p.add_argument("--w-value", type=float, dest="w_value")
    p.add_argument("--w-quality", type=float, dest="w_quality")
    p.add_argument("--w-momentum", type=float, dest="w_momentum")
    p.add_argument("--w-lowrisk", type=float, dest="w_lowrisk")
    p.add_argument("--quick", action="store_true", help="財務の取得期間を短縮した軽量実行")
    p.add_argument("--no-cache", action="store_true", help="キャッシュを使わない")
    p.add_argument("--no-sector-neutral", action="store_true", help="業種内正規化をやめる")
    p.add_argument("--self-test", action="store_true", help="合成データでロジックを検証 (API 不要)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    if args.self_test:
        return self_test()

    cfg = build_config(args)
    try:
        client = JQuantsClient(cfg)
        asof = resolve_asof(client, cfg)
        data = fetch_frames(client, cfg, asof)

        pf, k_long = compute_price_factors(data["prices"], asof, cfg)
        fins = prepare_fins(data["fins"], asof)
        fin = extract_financials(fins)
        div = dividend_signals(fins)
        t = build_table(data["master"], pf, k_long, fin, div, cfg)

        lag = (datetime.now().date() - datetime.strptime(asof, "%Y-%m-%d").date()).days
        paths = write_outputs(t, cfg, asof, {"lag_days": lag})
    except JQuantsError as exc:
        print("\n--- J-Quants API エラー ---", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    eligible = int(t["Eligible"].sum())
    print()
    print("=" * 72)
    print(f"評価基準日 : {asof}  (実行日の {lag} 日前 / Free プランの 12 週遅延)")
    print(f"ユニバース : {len(t):,} 銘柄  ->  ゲート通過 {int(t['PassedGates'].sum()):,}")
    print(f"モメンタム除外 : {int(t['ExcludedByMomentum'].sum()):,} 銘柄")
    print(f"最終候補   : {eligible:,} 銘柄  (うち増配 {int(t.loc[t['Eligible'], 'DivIncrease'].sum()):,})")
    print("-" * 72)
    for key, path in paths.items():
        print(f"{key:6s} : {path}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
