# Auris Flow 固定发布包部署说明

本目录是发布工作流生成的 **Linux 单机 Docker Compose 部署包**，不是源码 checkout。
`production/compose.yaml` 已移除所有 `build:` 并把每个第一方、第三方镜像固定到
`TAG@sha256:DIGEST`。不要用源码仓库中的候选 Compose、`latest` 或手工改写的镜像引用替换它。

该部署形态不提供节点级高可用或宿主机故障自动切换。开始前请确认 Docker Engine 26+、
Docker Compose plugin 2.27+、Python 3.12+、可信 TLS 证书、企业 OIDC 配置、外部 embedding
 endpoint、独立加密备份目标、Cosign 2.5.3+ 和至少 16 GiB RAM。

## 1. 验证下载与包内绑定

从官方 GitHub Release 同时下载部署 tar、tag 对应的 release-metadata Sigstore bundle、
`SHA256SUMS` 和 `SHA256SUMS.sigstore.json`。在尚未解压的下载目录中，用你从可信渠道确认的
release tag 执行：

```bash
set -euo pipefail
RELEASE_TAG=v1.0.0 # 必须替换为实际下载的精确 tag
cosign verify-blob \
  --bundle SHA256SUMS.sigstore.json \
  --certificate-identity \
  "https://github.com/auris-flow/auris-flow/.github/workflows/release-images.yml@refs/tags/${RELEASE_TAG}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS
grep "  auris-flow-${RELEASE_TAG}-deployment.tar.gz$" SHA256SUMS \
  | sha256sum --check -
grep "  auris-flow-${RELEASE_TAG}-release-metadata.sigstore.json$" SHA256SUMS \
  | sha256sum --check -
tar -xzf "auris-flow-${RELEASE_TAG}-deployment.tar.gz"
install -m 0444 \
  "auris-flow-${RELEASE_TAG}-release-metadata.sigstore.json" \
  "auris-flow-${RELEASE_TAG}-deployment/production/release-metadata.sigstore.json"
cd "auris-flow-${RELEASE_TAG}-deployment"
```

不得从 tar 内的 README 或 metadata 推导外层证书身份；上述官方仓库 workflow identity 是安装信任锚。
随后验证包内 metadata 的独立 Sigstore bundle 与全部内部绑定：

```bash
python3 scripts/release_bundle.py verify --bundle-root . --verify-signature
```

校验器先以固定的官方 tag workflow identity 验证
`production/release-metadata.sigstore.json`，再检查 metadata schema、release tag、完整 source commit、
Compose SHA-256、image-lock SHA-256、恢复兼容策略 SHA-256，以及 Compose 中每个服务与 image lock
的精确 digest 映射。任一文件缺失、篡改、包含 `build:`、使用可变镜像或出现路径逃逸都会
fail closed。当前首发策略没有前序 release，因此跨 commit 恢复默认被拒绝。

## 2. 配置 secret、TLS 与 OIDC

从部署包根目录执行：

```bash
cp production/.env.example production/.env
chmod 600 production/.env
${EDITOR:?set EDITOR} production/.env
bash production/scripts/init-secrets.sh

install -d -m 0700 production/tls
install -m 0600 /secure/source/fullchain.pem production/tls/fullchain.pem
install -m 0600 /secure/source/privkey.pem production/tls/privkey.pem
```

必须替换示例 FQDN、外部回调、embedding endpoint/model/dimension 和 OIDC 配置。默认使用随包
Keycloak 参考 IdP。若企业 IdP 需要 confidential client，只能把 client secret 写入 Docker
secret file，并额外使用随包 `production/compose.oidc-confidential.yaml`；禁止把 secret 放进 `.env`。
详细边界见 [生产配置说明](production/README.md)与
[密钥轮换 Runbook](doc/runbooks/key-rotation.md)。

## 3. 预检、拉取并启动固定镜像

标准 public PKCE client 使用：

```bash
python3 scripts/verify_production_compose.py --release \
  --compose-file production/compose.yaml \
  --env-file production/.env

docker --context default compose --project-name auris-flow \
  --project-directory production \
  --env-file production/.env \
  --file production/compose.yaml config --quiet

docker --context default compose --project-name auris-flow \
  --project-directory production \
  --env-file production/.env \
  --file production/compose.yaml pull

docker --context default compose --project-name auris-flow \
  --project-directory production \
  --env-file production/.env \
  --file production/compose.yaml up -d --wait
```

confidential client 部署在上述每条 `docker --context default compose --project-name auris-flow`
命令末尾的子命令前追加：

```text
--file production/compose.oidc-confidential.yaml
```

例如配置校验应以 `--file production/compose.yaml --file
production/compose.oidc-confidential.yaml config --quiet` 结束。不要把 override 单独启动。

## 4. 验收与运维

```bash
docker --context default compose --project-name auris-flow \
  --project-directory production \
  --env-file production/.env \
  --file production/compose.yaml ps

curl --fail --silent --show-error https://YOUR_AURIS_HOST/healthz
curl --fail --silent --show-error https://YOUR_AURIS_HOST/readyz
```

`/healthz` 只表示进程存活；`/readyz` 必须对 OIDC、MySQL、Redis、MinIO、Qdrant 与真实
Dagster 全部严格通过。`/metrics` 不应经公网 edge 暴露。

备份维护窗口中，先按 Runbook 停止写入服务，再执行：

```bash
bash production/scripts/backup.sh \
  --output-root /absolute/encrypted-backups \
  --storage-boundary encrypted-external
```

备份脚本会重新验签包内 metadata、读取 tag/commit，并验证当前 Compose project 中全部正在运行的
release service 实际镜像 digest（四个数据依赖为必选），再把 metadata 与运行镜像证据写入备份
manifest；它不依赖 `.git`。恢复与演练见
[备份恢复 Runbook](doc/runbooks/backup-restore.md)。升级、回滚、告警和事件响应入口：

- [升级与回滚](doc/runbooks/upgrade-rollback.md)
- [SLO、告警与故障排查](doc/runbooks/operations.md)
- [安全事件响应](doc/runbooks/security-incident-response.md)
- [版本与兼容策略](doc/release/versioning-and-compatibility.md)

计划停机只使用同一 Compose 文件执行 `down`，不要加 `--volumes`：

```bash
docker --context default compose --project-name auris-flow \
  --project-directory production \
  --env-file production/.env \
  --file production/compose.yaml down
```
