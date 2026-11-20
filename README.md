# OCI Image Unpacker

离线排查容器文件缺失时，从交付镜像还原最终根文件系统的服务。
上传仅含单个 manifest 的 OCI Image Layout tar，当次请求返回还原后的 `rootfs.tar`；
离线部署时还可以在 `/docs` 调用 `POST /volume-seed`，一次拿到根文件系统与各挂载卷的
初始化数据。

## 快速开始（docker compose）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，打开 <http://localhost:8000/docs>：

1. 先调 `GET /sample-image` 下载样例镜像 `sample-image.tar`（Swagger 里点 "Download file"）；
2. 再在 `POST /unpack` 里上传该 tar，响应即为 `rootfs.tar`；
3. 或在 `POST /volume-seed` 里上传同一 tar，响应为部署包 `seed-bundle.tar`。

命令行等价操作：

```bash
curl -OJ http://localhost:8000/sample-image          # 下载样例镜像
curl -F "file=@sample-image.tar" -OJ \
     http://localhost:8000/unpack                    # 得到 rootfs.tar
tar -tf rootfs.tar                                   # 查看还原结果

curl -F "file=@sample-image.tar" -o seed-bundle.tar \
     http://localhost:8000/volume-seed               # 根文件系统 + 卷种子
tar -tf seed-bundle.tar                              # rootfs.tar、各个卷 tar、mounts.json
```

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/unpack` | 上传 OCI Image Layout tar（单 manifest），返回 `rootfs.tar`；任何校验/解包失败返回 400，不返回残缺文件系统 |
| `POST` | `/volume-seed` | 同一份上传：校验、叠层后读取 `config.Volumes`，返回 `seed-bundle.tar`（`rootfs.tar` + 每个卷的种子 tar + `mounts.json`）；任何失败返回 400 |
| `GET` | `/sample-image` | 下载样例镜像（含删除、opaque、硬链接覆盖、两级卷与跨卷硬链接） |
| `GET` | `/healthz` | 健康检查 |

## 校验与还原语义

- **完整性校验**：index.json 引用的 manifest blob、manifest 引用的 config 与
  各 layer blob 都会核对**存在性、字节大小、SHA-256**；每个 layer 解压后
  （gzip 层先解压）再与 config 中 `rootfs.diff_ids` 逐一核对。层数与
  diff_ids 数量不一致、摘要不符、缺块、损坏，整次请求报错。
- **层格式**：支持未压缩层（`...layer.v1.tar`）与 gzip 层
  （`...layer.v1.tar+gzip`，含 docker 对应 mediaType）。
- **按清单层序还原**：普通文件、目录、符号链接、硬链接与权限位（含
  setuid/sgid/sticky）；目录权限与 mtime 在全部层应用完后统一设置，避免
  只读目录阻断同层/上层写入。
- **whiteout / opaque**：每层先扫描全部条目，把 `.wh.<name>` 与
  `.wh..wh..opq` 的删除动作**只作用于下层文件系统**，然后再写入本层新
  内容。因此删除标记在层内的位置（文件前/后）不会影响同层新增文件。
- **硬链接覆盖安全**：新文件先写临时文件再 `rename` 替换，绝不原地截断，
  所以覆盖硬链接的一端时，另一端保留原字节。输出 tar 中仍共享 inode 的
  硬链接会以硬链接条目保留。
- **路径安全**：条目路径拒绝绝对路径与 `..`；符号链接目标（绝对目标按
  rootfs 内解释）与硬链接目标经解析后必须落在隔离目录内，写入前再随
  符号链接逐层解析校验，任何逃逸都会让整次请求失败。
- **请求隔离**：每个请求使用独立临时目录，失败即清理，成功响应发送后
  清理；请求之间互不影响。

## 卷种子（POST /volume-seed）

离线部署时容器的根文件系统与挂载卷要分开准备：卷通常以空卷启动，需要用镜像
自带的数据初始化。`/volume-seed` 沿用 `/unpack` 的全部校验（blob 存在性、大小、
SHA-256、`rootfs.diff_ids`）与叠层语义，并额外从**已校验**的镜像配置读取
`config.Volumes`。

响应是单个 tar（`seed-bundle.tar`），内含：

- `rootfs.tar`：最终根文件系统；
- 每个声明卷一个种子 tar（文件名由挂载路径去掉前导 `/`、把 `/` 换成 `_` 得到，
  例如 `/srv/data` → `srv_data.tar`）；
- `mounts.json`：挂载列表，父卷排在子卷前：

  ```json
  {"mounts": [{"path": "/srv/data", "file": "srv_data.tar"},
              {"path": "/srv/data/cache", "file": "srv_data_cache.tar"}]}
  ```

规则：

- 每个声明路径必须是**最终树**中非根的绝对目录：路径的每一级（含最后一级）都
  必须是真实目录，途经符号链接直接判错（不做跟随），使种子包与部署时的实际
  挂载点严格一致；不满足则整次请求 400，不返回任何包；
- 卷种子 tar 内的路径从**卷根算起**，解到挂载点即可使用；
- `rootfs.tar` 保留镜像自带的卷内数据，但在每个**下一级挂载点**只留空目录（不再
  向下展开），避免镜像内容遮蔽随后挂载的卷；
- 每个包都能**独立解开**，并保留包内硬链接；硬链接组跨多个包时，每个含该组成
  员的包各自存一份正文，组内关系在包内仍然保留；
- 符号链接文本原样照录；
- 只暂存一份展开后的 rootfs 树，出包时按块读写，不重复展开。

部署示例（把卷种子解到宿主目录、根文件系统解到目标目录）：

```bash
tar -xf seed-bundle.tar
mkdir -p /srv/rootfs /srv/vol/data /srv/vol/cache
tar -xf rootfs.tar -C /srv/rootfs          # 卷挂载点此时只有空目录
tar -xf srv_data.tar -C /srv/vol/data
tar -xf srv_data_cache.tar -C /srv/vol/cache
```

## 样例镜像内容

两层（第一层 gzip，第二层未压缩），用于验证上述语义：

- 下层文件 `app/config.txt`、`app/data/remove.txt` 被 whiteout 删除；
- `secret/` 被标记 opaque：下层的 `secret/token.txt` 消失，同层新增的
  `secret/public.txt` 保留；
- `app/data/rebuilt.txt` 在同层先写新内容、后出现其 whiteout —— 新内容
  必须保留；
- `bin/tool` 与 `bin/tool-link` 在下层是同一硬链接，上层只覆盖
  `bin/tool`（`tool-v2`），`bin/tool-link` 必须仍是 `tool-v1`；
- 相对符号链接 `bin/tool-alias -> tool` 与绝对符号链接
  `etc/motd-link -> /etc/motd`；
- 权限位：`bin/tool` 0755、`secret/` 0700；
- 两级卷：`config.Volumes` 声明 `/srv/data` 与 `/srv/data/cache`（`/srv/config.yml`
  是卷外的镜像内容）；`srv/data/shared.bin` 与 `srv/data/cache/shared.bin` 是同一
  硬链接（**跨卷硬链接组**），`srv/data/cache/notes.txt` 与 `srv/data/cache/twin.txt`
  是卷内硬链接组。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `UNPACK_WORKDIR` | `$TMPDIR/oci-unpacker` | 请求临时目录与样例镜像存放位置 |
