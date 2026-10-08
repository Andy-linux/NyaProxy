# 验证记录 · 2026-10-08

基线：NyaProxy v0.8.2 / 21db6859c2d89420731af80747670c2a530634f4。
分支：feat/nai-utility-compat。

- 完整回归：413 passed，124.12 秒。
- 最后补强 Bearer 必须存在的校验后，兼容 HTTP 集成测试重跑：15 passed，14.75 秒。
- 新增测试共 21 项（15 项真实 HTTP 集成 + 6 项边界单元测试）。
- Ruff 检查与格式检查通过。
- 配置文件通过打包内 JSON Schema 验证。
- 直接 TLS 实测：用临时自签测试证书作为受信 CA，客户端保持证书校验，health=200、info=404、无认证生成=403。
- Python sdist 和 wheel 通过隔离环境构建。
- Compose YAML 解析通过；环境无 Docker，未运行容器构建。

完整回归的唯一 warning 为上游已有 Starlette TestClient/httpx 废弃提示，非测试失败。
真实 HTTP 集成测试连接本机模拟上游，不使用真实 NovelAI Key、不产生生成费用。
测试覆盖 SSE 分段实时到达、流结束后的独占 Key 释放，以及客户端和配置身份头不外泄。
此处的 ZIP 字节为模拟数据，验证透传字节不变，不等于真实图片生成成功。

尚未验证：真实 NAI 生成、真实 Windows 客户端与公网服务器联调、Docker 实际构建。
GitHub 远端 Fork 尚未创建：当前连接没有 Fork/建仓库接口。交付包含完整 Git bundle
和发布脚本，改造可继续推送到用户 Fork。没有改动任何已有用户远端仓库。
