# Google Apps Script から Cloud Run を呼び出す: カスタムオーディエンス設定

この資料は、Google Apps Script (GAS) の `ScriptApp.getIdentityToken()` を使い、IAM 認証が必須の Cloud Run サービスを呼び出すための参考手順です。Tailscale / Secret Manager / Cloud Run の初期セットアップは [README](../README.md) を参照してください。

## 背景

Cloud Run の認証付きサービスは Google 署名の OpenID Connect ID トークンを受け付けます。トークンの `aud` (audience) は、原則として呼び出し先 Cloud Run サービスの標準 URL と一致する必要があります。

一方、GAS の `ScriptApp.getIdentityToken()` で得られるトークンの `aud` は、GAS が使用する OAuth クライアント ID です。GAS からこのトークンをそのまま送信する場合、Cloud Run 側にこのクライアント ID を**カスタムオーディエンス**として追加します。

**注意:** カスタムオーディエンスはトークンの受け入れ条件を増やす設定であり、IAM 認可を回避するものではありません。呼び出し主体には別途 `roles/run.invoker` が必要です。カスタムオーディエンスには、信頼する GAS のクライアント ID だけを登録してください。

## 1. GAS の OAuth スコープ

GAS エディタの **プロジェクトの設定**で `appsscript.json` を表示し、既存の `oauthScopes` に必要なスコープを含めます。

```json
{
  "timeZone": "Asia/Tokyo",
  "runtimeVersion": "V8",
  "oauthScopes": [
    "openid",
    "https://www.googleapis.com/auth/script.external_request",
    "https://www.googleapis.com/auth/userinfo.email"
  ]
}
```

これは説明用の最小例です。既存のマニフェスト全体を上書きせず、ほかの機能が必要とするスコープや設定を保持してください。

- `openid`: `ScriptApp.getIdentityToken()` の利用に必要
- `script.external_request`: `UrlFetchApp.fetch()` の利用に必要
- `userinfo.email`: ID トークンにユーザーのメールアドレス情報を含めるために使用

スコープ変更後は、GAS を再実行して必要な承認を行います。

## 2. GAS の ID トークンから audience を確認

以下を一度実行し、実行ログに表示される `aud` の値（OAuth クライアント ID）を控えます。

```javascript
function logIdentityAudience() {
  const token = ScriptApp.getIdentityToken();
  if (!token) throw new Error("IDトークンを取得できませんでした");

  const encodedPayload = token.split(".")[1];
  const payload = JSON.parse(
    Utilities.newBlob(
      Utilities.base64DecodeWebSafe(encodedPayload)
    ).getDataAsString("UTF-8")
  );

  console.log("Audience: " + payload.aud);
  console.log("Token has email claim: " + Boolean(payload.email));
}
```

JWT のペイロードをデコードしているだけで、署名検証ではありません。**ID トークンそのものはログに出力しないでください。** `aud` は通常 `...apps.googleusercontent.com` の形式です。

## 3. Cloud Run のカスタムオーディエンスに追加

Cloud Shell 等で、サービス名・リージョン・プロジェクトを実環境に合わせて指定します。

```bash
PROJECT_ID="your-project-id"
REGION="europe-west1"
SERVICE="your-cloud-run-service"
GAS_AUDIENCE="xxxxxxxx.apps.googleusercontent.com"

gcloud run services update "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --add-custom-audiences="$GAS_AUDIENCE"
```

`--add-custom-audiences` は既存のカスタムオーディエンスを残して追加します。Cloud Run の標準 URL も引き続き受け入れられます。これはサービスレベルの設定で、更新時には新しいリビジョンが作成されます。

設定を確認するには、次を実行します。

```bash
gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format=export
```

出力内のサービスメタデータにある `run.googleapis.com/custom-audiences` アノテーションを確認してください（`spec.template.metadata` のアノテーションではありません）。

## 4. Cloud Run Invoker を付与

Cloud Run の **権限**で、GAS の実効ユーザー（ID トークンの主体）に対し、対象 Cloud Run サービスの `roles/run.invoker` を付与します。

この権限は、Cloud Run **実行用サービスアカウント**に付ける `roles/secretmanager.secretAccessor` とは別物です。GAS の実行形態によって実効ユーザーが変わり得るため、エディタからの手動実行、トリガー実行、ウェブアプリ実行など、実際の呼び出し形態で確認してください。

Cloud Run は引き続き **Require authentication** にします。GAS から標準の `run.app` URL に直接接続する場合は、Cloud Run の ingress 設定も到達可能な状態にしてください。

## 5. GAS から /health を呼び出して検証

```javascript
function testCloudRunHealth() {
  const baseUrl = "https://YOUR-SERVICE-URL.run.app";
  const token = ScriptApp.getIdentityToken();
  if (!token) throw new Error("IDトークンを取得できませんでした");

  const response = UrlFetchApp.fetch(baseUrl + "/health", {
    method: "get",
    headers: { Authorization: "Bearer " + token },
    muteHttpExceptions: true
  });

  const status = response.getResponseCode();
  const body = response.getContentText();
  console.log("HTTP status: " + status);
  console.log("Response: " + body);

  if (status !== 200) {
    throw new Error("Cloud Run呼び出し失敗: HTTP " + status + " / " + body);
  }
  return JSON.parse(body);
}
```

期待する応答例:

```json
{"status":"ok","tailscale":{"connected":true,"backend_state":"Running"}}
```

HTTP 認証が通っても Tailscale が未接続なら `/health` は HTTP 503 を返すため、レスポンスの内容も確認します。

## トラブルシューティング

| 現象 | まず確認すること |
| --- | --- |
| `ScriptApp.getIdentityToken()` が null | `openid` スコープと GAS の再承認 |
| Cloud Run が HTTP 401 を返す | ID トークンが正しいか、`aud` がカスタムオーディエンスに登録されているか |
| Cloud Run が HTTP 403 を返す | 呼び出し主体の `roles/run.invoker`、認証必須設定、ingress |
| Cloud Run が HTTP 503 を返す | Cloud Run まで到達した場合は `/health` の Tailscale 接続状態、Revision ログ |
| GAS の設定変更後に動かなくなった | OAuth スコープ、実効ユーザー、GAS のクライアント ID と Cloud Run 側 audience を再確認 |

認証失敗時の HTTP コードは構成により異なる場合があります。401/403 だけで断定せず、Cloud Run のリクエストログも参照してください。

## 公式資料

- [Google Apps Script: Google Cloud サービスに接続する](https://developers.google.com/apps-script/guides/services/cloud-run?hl=ja)
- [Cloud Run: カスタムオーディエンスを設定する](https://docs.cloud.google.com/run/docs/configuring/custom-audiences?hl=ja)
- [Apps Script: Google Cloud プロジェクト](https://developers.google.com/apps-script/guides/cloud-platform-projects)
