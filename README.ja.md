# codex-app-mcp

[English](README.md)

Python 実装のローカル MCP Server。公式 Codex SDK（`openai-codex`）経由で Codex App Server の thread / turn 操作を MCP tool `codex` / `codex-reply` として公開する、**非対話・主要 API 互換サブセット**（v0.1）。

## このプロジェクトを作った理由

Codex CLI が従来の MCP Server Mode を廃止したことで、OpenCode のように公式の
Codex 連携が提供されていないツールから、Codex を協働エージェントとして使うことが
難しくなりました。このプロジェクトは、公式 Codex SDK を利用した限定的で安全側に
倒す bridge として、その利用経路を再び提供するためのものです。

## 1. 安全性

- MCP transportはstdioのみ
- 承認要求（command実行・file変更）は即時拒否（fail-closed）。`approval-policy=never`専用
- 既定sandboxは`read-only`。`danger-full-access`は受理しない
- 1 bridge processにつきactive turnは1件。暗黙の待ち行列なし
- 実行済みか不明なturnは自動再送しない
- `allowed_roots`の既定値は空で、すべてのworkspaceを拒否する
- ログはstderrへ出力し、prompt・回答・認証情報・file内容を記録しない

本bridgeは汎用App Server gatewayではありません。対話承認、実行中turnへのsteer、
HTTP transportはv0.1の対象外です。

## 2. 動作要件

| 要件 | 確認 |
|---|---|
| Python 3.14.x（開発majorは`.python-version`参照） | `codex-app-mcp doctor` |
| [uv](https://docs.astral.sh/uv/) | `uv --version` |
| Codex 認証済み（`~/.codex/auth.json` 等） | `codex-app-mcp doctor` が存在のみ確認 |
| ネットワーク: モデル推論に OpenAI への接続 | doctor の catalog 確認 |

依存は完全固定: `openai-codex==0.154.0`（SDK 同梱の `codex app-server` を利用）、`mcp==2.2.0`。PATH 上の他 `codex` は使用しない。

## 3. クイックスタート

公開版はrepositoryをcloneせず、対象projectから直接実行できます（最初の実行では
固定runtimeを含め約120 MiBをdownloadする場合があります）。

```sh
cd /absolute/path/to/target-project
uvx --from git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1 \
  codex-app-mcp init
uvx --from git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1 \
  codex-app-mcp doctor
```

サポート対象は最新安定majorのPython 3.14系のみ。`pyproject.toml`で
`>=3.14,<3.15`を要求し、`.python-version`は3.14系の最新利用可能patchを選ぶ。

doctor は SDK/runtime・認証の有無・自己接続の疑い・policy・モデル catalog を確認する。`ok: true` になれば使用可能。

## 4. 設定

利用するrepositoryで最小設定を生成する:

```sh
cd /absolute/path/to/target-project
uvx --from git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1 \
  codex-app-mcp init
```

Git root（なければ現在のdirectory）へ`bridge.toml`を作成し、そのworkspaceだけを
`allowed_roots`へ登録する。既存fileは変更しない。ユーザー共通設定は
`codex-app-mcp init --global`、任意の生成先は`--config PATH`を使う。

`init --global`でも許可対象は現在のGit root（なければ現在directory）です。
ホームdirectory全体など広すぎるrootは安全のため拒否します。別のworkspaceを明示する
場合は`--root /absolute/path/to/workspace`を指定し、複数rootは生成後の
`allowed_roots`へ追加します。

```toml
[defaults]
model = "gpt-5.6-terra"
effort = "medium"
sandbox = "read-only"
approval_policy = "never"

[policy]
# cwd / thread 受け入れ先の制限（ファイル読み取りの sandbox ではない）
allowed_roots = ["/absolute/path/to/repository"]
allowed_models = ["gpt-5.6-terra"]
allowed_sandboxes = ["read-only", "workspace-write"]

[limits]
turn_timeout_seconds = 900

[logging]
level = "INFO"
format = "json"
```

- `allowed_roots` が空だと全 workspace が拒否される（fail-closed）。必ず設定すること。
- `defaults.cwd`は通常不要。tool引数で未指定ならbridge起動directoryを使う。clientの
  起動directoryが不安定な場合のみ明示する。
- 既知 section 内の未知キー（typo）は**起動時エラー**になる。
- 全キー・既定値・環境変数・CLI 上書き: `docs/configuration.md` 参照。

設定の探索順は次のとおりです。

1. 明示した`--config PATH`
2. bridge起動directoryから親方向に探索した最寄りの`bridge.toml`
3. `$XDG_CONFIG_HOME/codex-app-mcp/bridge.toml`などのユーザー設定
4. fail-closedな組み込み既定値

個々の設定値は`CLI > CODEX_APP_MCP_*環境変数 > TOML > 組み込み既定値`の順で
解決します。

## 5. MCP client 登録

**shell 文字列ではなく executable と引数配列で登録すること。**

### Claude Code

```sh
claude mcp add codex -- \
  uvx --from git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1 \
  codex-app-mcp serve
```

`~/.claude.json` 相当（JSON）:

```json
{
  "mcpServers": {
    "codex": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1",
        "codex-app-mcp", "serve"
      ]
    }
  }
}
```

### OpenCode（`.opencode.json` / グローバル設定）

```json
{
  "mcp": {
    "codex": {
      "type": "local",
      "command": [
        "uvx", "--from",
        "git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1",
        "codex-app-mcp", "serve"
      ],
      "enabled": true
    }
  }
}
```

注意:

- shell文字列ではなくexecutableと引数配列として登録する。
- PyPIには公開しない。GitHubのrelease tag `v0.1.1`を固定する。
- 自動探索はbridge起動directoryから親へ向かう。固定したい場合は従来どおり
  `serve --config /absolute/path/to/bridge.toml`を指定できる。
- Codex 側（`~/.codex/config.toml` の `mcp_servers`）にこの bridge を登録すると再帰的自己接続になり得る。bridge は子 runtime 環境の `CODEX_APP_MCP_CHILD=1` で起動拒否するが、その構成自体を推奨しない。

## 6. 利用方法

### 6.1 新規実行（`codex`）

```json
{
  "prompt": "このリポジトリの README を要約して。",
  "model": "gpt-5.6-terra",
  "effort": "medium",
  "cwd": "/absolute/path/to/repository",
  "sandbox": "read-only"
}
```

- `prompt` のみ必須。他は省略時は起動設定・runtime 既定を継承。
- 旧引数（`approval-policy`・`config`・`base-instructions`・`developer-instructions`・`compact-prompt`）はサブセットで受理（`docs/tools.md`）。

成功応答:

```json
{
  "content": [{"type": "text", "text": "要約本文..."}],
  "structuredContent": {"threadId": "01a0...", "content": "要約本文..."},
  "isError": false,
  "_meta": {"codex-app-mcp": {"turnId": "01a0...", "status": "completed",
                              "requestedModel": null, "requestedEffort": null}}
}
```

実際の `content` には、回答本文に続いて `structuredContent` をJSON化した第2 text
blockも入る。これは `structuredContent` をモデルへ公開しないMCP clientでも
`threadId` を取得できるようにする互換出力である。

### 6.2 継続実行（`codex-reply`）

```json
{"prompt": "さらに日本語でも書いて。", "threadId": "01a0..."}
```

- `conversationId` は旧互換 alias（両方指定時は同値のみ受理）。
- `model` / `effort` は以降の turn に維持される拡張指定。
- 実行中の thread への呼び出しは `THREAD_BUSY`。

### 6.3 キャンセル・timeout

- client 側で request を取り消すと、bridge は当該 turn に `turn/interrupt` を送り、取消済み request への結果送信を行わない。
- 1 turn の既定上限は 900 秒（`turn_timeout_seconds`）。超過時は interrupt 後 `TURN_TIMEOUT`（副作用が残り得る旨を `mayHaveSideEffects` で明示）。
- **client の tool timeout は bridge の turn timeout + cleanup 猶予より長く**設定すること。

### 6.4 主なエラーと対処

| コード | 意味 | 対処 |
|---|---|---|
| `SERVER_BUSY` / `THREAD_BUSY` | active turn が 1 件の上限に達している | 実行完了またはキャンセル後に再呼び出し（retryable） |
| `WORKSPACE_DENIED` | cwd が `allowed_roots` 外 | `bridge.toml` の `allowed_roots` / call の `cwd` を修正 |
| `MODEL_NOT_ALLOWED` / `MODEL_UNAVAILABLE` | operator 許可外 / catalog に不存在 | `allowed_models` またはモデル指定を修正。自動 fallback なし |
| `UNSUPPORTED_EFFORT` | モデルが effort 非対応 | `doctor` で対応 effort を確認 |
| `OUTPUT_LIMIT_EXCEEDED` | 回答が `max_result_bytes` 超 | `max_result_bytes` 拡大またはタスク分割。成功回答の切断は行わない |
| `EXECUTION_STATE_UNKNOWN` | 実行成否不明（transport 断等） | **同じ turn を再送しない**。必要なら新規 prompt で確認 |
| `UNSUPPORTED_SERVER_REQUEST` | runtime が未対応の承認要求を送出 | runtime は停止済み。bridge 再起動後に再試行 |
| `RUNTIME_STOP_UNCONFIRMED` | runtime停止を確認できない | 同一processでは再開不可。bridgeを再起動し、残存processを確認 |

## 7. ログと診断

- 診断は stderr に JSON（既定）。prompt・回答本文・token は記録されない。
- `doctor` で SDK/runtime・認証・policy・catalog を確認できる。
- トラブル時は `[logging] level = "DEBUG"` で詳細採取。

## 8. 開発

```sh
aqua install
aqua exec -- uv sync --frozen --all-groups
aqua exec -- uv run --frozen pytest -m 'not live' -q
aqua exec -- uv run --frozen ruff check src tests
aqua exec -- uv run --frozen ruff format --check src tests
aqua exec -- uv build
aqua exec -- betterleaks dir .
aqua exec -- betterleaks git . --platform github
```

`pyproject.toml` の `addopts = "-m 'not live'"` により、`pytest` 単体では
live 試験が選択されません。

live試験は実認証・実runtime・実推論・sandbox内のfile操作・cancel・process終了を
伴うため、明示的な許可がある場合だけ実行します。

```sh
aqua exec -- uv run --frozen pytest -m live -q
```

- テスト構成と CT 対応表: `docs/testing.md`
- 偽 App Server harness: `tests/contract/fake_app_server.py`

## 9. 旧 MCP 実装からの移行

旧 `codex mcp-server`（rust v0.153 系）からの主な差分:

| 項目 | 旧 | 本 bridge v0.1 |
|---|---|---|
| `codex` / `codex-reply`・主要引数・出力形式 | − | 互換（サブセット） |
| `conversationId` alias | threadId 優先で黙って採用 | 矛盾する二重指定は拒否 |
| 任意 `config` | 受理 | allowlist 3 キーのみ |
| `approval-policy=on-request` | 受理 | 明示拒否 |
| `danger-full-access` | 受理 | 明示拒否 |
| 実行中 turn への steer | あり | なし（`THREAD_BUSY`） |
| `codex/event` 独自通知 | あり | 標準 `notifications/progress` のみ |
| model / effort の reply 指定 | 一部 | 拡張として対応 |

移行手順: (1) 旧 `codex` MCP server 登録を解除、(2) 本 README §5 で再登録、(3) `doctor` で確認。

## 10. ドキュメント

| 文書 | 内容 |
|---|---|
| `docs/README.md` | 文書の正本・読み順・保守方針 |
| `docs/architecture.md` | 構成・状態管理・非同期構造 |
| `docs/tools.md` | MCP tool 契約 |
| `docs/model-and-effort.md` | model / effort 解決 |
| `docs/execution.md` | 実行フロー・キャンセル・timeout |
| `docs/security.md` | 承認・sandbox・policy・自己接続防止 |
| `docs/errors.md` | エラー契約 |
| `docs/configuration.md` | 設定リファレンス |
| `docs/logging.md` | ログ仕様 |
| `docs/testing.md` | テスト仕様と CT 対応表 |
| `bridge.example.toml` | 設定例 |
| `src/codex_app_mcp/schemas/tools.json` | 配布・実行時のtool schema正本 |
