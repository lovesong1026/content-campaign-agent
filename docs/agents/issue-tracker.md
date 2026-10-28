# Issue tracker: GitHub

本项目的需求与实施任务使用 GitHub Issues 跟踪。

- Repository: lovesong1026/content-campaign-agent
- URL: https://github.com/lovesong1026/content-campaign-agent
- 使用 gh CLI；命令显式指定 --repo，不依赖当前目录的 Git remote。

## 操作约定

- 创建：gh issue create --repo lovesong1026/content-campaign-agent --title "<标题>" --body-file "<正文文件>"
- 读取：gh issue view <编号> --repo lovesong1026/content-campaign-agent --json number,title,body,labels,comments
- 列表：gh issue list --repo lovesong1026/content-campaign-agent --state open --json number,title,body,labels
- 评论：gh issue comment <编号> --repo lovesong1026/content-campaign-agent --body-file "<正文文件>"
- 更新正文：gh issue edit <编号> --repo lovesong1026/content-campaign-agent --body-file "<正文文件>"
- 关闭：gh issue close <编号> --repo lovesong1026/content-campaign-agent

多行正文先写入临时文件，再通过 --body-file 提交。
需求拆为多个实施任务时，在正文中明确父任务及阻塞关系。

## 技能指令映射

- “publish to the issue tracker”：创建 GitHub Issue。
- “fetch the relevant ticket”：读取指定 Issue 及其评论。

## Pull requests as a triage surface

PRs as a request surface: no.
