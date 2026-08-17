# ARX5 Operator UI

`beta1` 是纯前端视觉原型：不连接 CLI、ROS、设备、Station 配置或 Reports，也不挂载 Docker Socket。

## 本地开发

```bash
npm ci
npm run dev
```

## 正式镜像构建

从仓库根目录执行：

```bash
docker compose -f docker/compose.operator-ui.yaml up -d --build
```

服务仅监听宿主机 `127.0.0.1:4173`。容器无特权、只读运行，不使用 host network，也不挂载采集设备或数据目录。

## 离线工作站验证

工作站无法访问 npm registry 时，可在有依赖的开发机预构建 `dist/`，再将源码与产物复制到工作站：

```bash
cd apps/operator-ui
npm ci
npm run build
cd ../..
docker build --target prebuilt -t arx5-operator-ui:beta1 apps/operator-ui
docker compose -f docker/compose.operator-ui.yaml up -d --no-build
```

`prebuilt` 只用于离线验收；正式发布仍应使用锁文件从源码构建并发布固定版本镜像。

远程访问工作站时建立本机隧道：

```bash
ssh -N -L 4173:127.0.0.1:4173 w3-arx5
```

随后打开 `http://127.0.0.1:4173/`。
