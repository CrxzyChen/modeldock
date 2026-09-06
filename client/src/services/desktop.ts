export function desktopBridge(): MediaCenterDesktopBridge {
  const bridge = window.mediaCenterDesktop;
  if (!bridge) throw new Error("MediaCenter PC 客户端桥接不可用");
  return bridge;
}

export function hasDesktopBridge(): boolean {
  return Boolean(window.mediaCenterDesktop);
}
