"""固定测试环境，让测试结果不受运行时的环境变量影响。

起因是一次真实的翻车：部署脚本原本先跑测试、再加载 /etc/practice-xiaoda.env，
后来为了先做一次备份，把加载挪到了测试之前。测试于是继承了生产环境变量，
`PRACTICE_XIAODA_ENV=production` 一个变量就让 19 个用例报错——因为生产模式
会隐藏演示数据，而这些用例正靠演示数据构造场景。

代码没坏，测试也没坏，坏的是"测试结果取决于跑它的人当时 shell 里有什么"。
这种问题最难查：本地全绿、服务器全红，两边跑的是同一份代码。

所以在这里把测试依赖的配置钉死。`discover -s tests` 会先导入这个包，
因此它在任何测试模块加载之前生效。真要验生产行为的用例，自己在 setUp 里
临时改回去——显式声明比依赖外部环境可靠。
"""
import os

# 开发模式：生产模式会隐藏演示数据，并对管理接口强制鉴权。
os.environ["PRACTICE_XIAODA_ENV"] = "development"
# 演示数据必须既被种进去、又可见——大量用例靠它构造"库里有若干在招项目"
# 的场景。这两个变量缺一不可：SEED_DEMO_DATA 决定种不种，SHOW_DEMO_DATA
# 决定读的时候露不露。生产环境两个都是关的。
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["SHOW_DEMO_DATA"] = "true"
# 别让本机或服务器上配的写作模型把单测变成联网测试——又慢又不稳定，
# 需要验模型分支的用例会自己把 llm.is_enabled 打开。
os.environ.pop("DEEPSEEK_API_KEY", None)
# 限流按 token 指纹分桶。生产值是 600，本地默认 60；测试里几十个请求
# 用同一个 key 打过去，压在 60 上会随机撞到 429。
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "10000")
