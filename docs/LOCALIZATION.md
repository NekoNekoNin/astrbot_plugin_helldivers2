# 更新本地译名

插件随附的是有来源指纹的部分名称索引，日常查询不需要安装游戏。本地化更新需用自己的游戏与 Filediver 导出新的 strings JSON，再执行：

```sh
python scripts/import_game_localization.py --exports /path/to/exported-strings --manifest /path/to/steamapps/appmanifest_553850.acf --report import-report.json
```

脚本按同字符串 ID 配对英文 us 与简中 cn，保留构建号和资源哈希。完整游戏导出不加入 Git；同名冲突留待核对，缺项继续通过 Wiki 查证。
