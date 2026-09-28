# 配额继承与借用（从 0 实现）

## 一、范围

**要做的事**：从零实现一个配额树模拟器，只用 Python 3.13 标准库。读入一棵配额树与一份按逻辑时间排列的操作脚本，逐步模拟「申请 / 回收」，按固定格式输出每一步的余额与借用台账；再用一份自包含的 HTML（内联 SVG）把同样的过程画出来。

**交付物**：仓库根目录 `quota_sim.py` 是唯一入口：

```
python3 quota_sim.py --trees <树目录> --script <脚本文件> [--html <输出路径>]
```

文本结果写 stdout；给出 `--html` 时额外写一个单文件 HTML（生成物放 `var/`，它已在 `.gitignore` 中忽略）；诊断信息写 stderr。不引入任何第三方依赖，不需要构建步骤，不需要联网，不读环境变量与配置文件。

**不做的事**：

- 不做真实时间调度：超时只按脚本里的逻辑时间结算，不睡眠、不看墙上时钟；
- 借不到时不做部分放行，也不排队、不抢占、不做优先级；
- 不向兄弟或子节点借，也不把借来的额度再转借；
- 不做持久化、数据库、多进程 / 多线程、网络服务与鉴权；
- 不做命令行以外的交互界面。

## 二、口径与公式

每个节点 n 有固定额度 `quota(n)`，以及三个随运行变化的计数器：`own(n)`（自有额度中已被本节点占用的部分）、`debt(n)`（本节点当前欠各出借方的总额）、`lent(n)`（自有额度中已借给后代的部分）。恒有：

- `used(n) = own(n) + debt(n)`；
- `idle(n) = quota(n) - own(n) - lent(n)`，由构造方式保证 `idle(n) >= 0`；
- `free(n) = idle(n) + Σ idle(祖先)`，即「继承」可见的全部空闲额度。

申请 `APPLY n a`：若 `free(n) < a` 则整单拒绝、零状态变化；否则先占用 `idle(n)`，不足部分从父、祖父……逐级向上借，每个祖先最多借到它当时的 `idle`，每笔单独记账。

回收 `RELEASE n a`：若 `a > used(n)` 则整单拒绝；否则先按记录号从小到大偿还 n 的借用（一笔不够就继续下一笔），剩余部分再释放 `own(n)`。

超时：脚本给出 `timeout T`。每笔操作执行前先结算超时——所有满足 `t - start >= T` 的未还记录，按 `start` 升序、借方 id、记录号依次强制收回：把该笔 `remain` 从借方的 `debt` 中扣掉（`used` 随之下降），同时把额度还给出借方的 `lent`。`T = 0` 表示下一笔操作前就收回。

## 三、状态机与数据结构

- 配额树：每个节点有 `id`、父 `id`、`quota`、初始占用；只有一个根（父写 `-`），父节点必须先于子节点出现，初始视为 `debt = lent = 0`、`own = used <= quota`。
- 借用台账：每条记录是 `(记录号, 借方, 出借方, 额度, 未还余额, 起始时间)`；记录号自 `b1` 起按建立顺序递增，不合并、不复用，余额清零即移出台账。
- 每一步只有两段转移：先「结算超时」，再「执行操作」——成功则改状态，失败则不动状态。
- 任意时刻：`lent(x) = Σ remain(出借方为 x 的记录)`，`debt(x) = Σ remain(借方为 x 的记录)`，`own + lent + idle = quota`。

## 四、输入输出与文件格式

三份文本与 stdout 都是 UTF-8（无 BOM）、LF 换行的行式文本，`#` 起头为注释，空行忽略，字段用空白分隔。

**配额树** `samples/trees/<名字>.tree`：

```
node <id> <parent> <quota> <used>
```

`<id>` 取 `[A-Za-z0-9_.-]+` 且唯一；`<parent>` 是父 id，根写 `-`；`quota`、`used` 是不超过 10^9 的非负整数且 `used <= quota`。第一条有效行必须是根。

**操作脚本** `samples/scripts/<名字>.ops`：

```
tree <名字>
timeout <T>
<操作号> t=<t> <APPLY|RELEASE> <节点 id> <额度>
```

前两条有效行固定是 `tree` 与 `timeout`，`<名字>` 对应同目录下的 `<名字>.tree`；操作号唯一，`t` 非负且单调不减，额度是 1..10^9 的整数。

**输出**（stdout；每步 3 行，发生超时收回时在其前另加 `EVENT` 行）：

```
EVENT t=<t> action=RECLAIM id=<记录号> borrower=<id> lender=<id> amount=<额> start=<起始 t>
STEP t=<t> id=<操作号> action=APPLY node=<id> amount=<额> result=OK borrowed=<额>
STEP t=<t> id=<操作号> action=APPLY node=<id> amount=<额> result=REJECT reason=INSUFFICIENT available=<额>
STEP t=<t> id=<操作号> action=RELEASE node=<id> amount=<额> result=OK repaid=<额>
STEP t=<t> id=<操作号> action=RELEASE node=<id> amount=<额> result=REJECT reason=UNDERFLOW used=<额>
STATE t=<t> <id>=<used>/<quota>/<debt>/<lent> ...
DEBT t=<t> <记录号>=<借方>-><出借方>:<remain>/<额度>@<起始 t> ...
```

`STATE` 按树文件中的出现顺序列出全部节点，`DEBT` 按记录号升序列出未还记录，没有未还记录时整行为 `DEBT t=<t> -`，行尾不留空格。`borrowed` 是本次借入总量，`repaid` 是本次偿还总量（其余部分释放自有），`available` 是拒绝时算出的 `free`。

## 五、性能与验收口径

- 预算：50 个节点、5000 步的脚本，单进程 5 秒内跑完（stdout 重定向到文件），峰值内存不超过 256 MB；每步除输出外的新增开销应为 O(树高 + 本次新增记录数)，不得每步重建整棵借用图。
- 结果验收：`samples/expected/<名字>.out` 必须与 `python3 quota_sim.py --trees samples/trees --script samples/scripts/<名字>.ops` 的 stdout 逐字节一致，不多空行、不加提示。
- 报告验收：`--html` 产物是单个文件、离线可直接打开、无任何外部引用（不得出现 `<script src`、`<link`、`http://`、`https://`），至少内联一段 `<svg>`；每一步标出 `t`、操作号、动作、结果与该步各节点 `used/quota`，数值与文本输出一致。
- 错误处理：输入非法（重复 id、未知父节点、额度越界、`t` 倒退等）时退出码 2，stderr 打印 `ERROR <行号> <说明>`；正常跑完退出码 0（含出现 REJECT 的情况）。

## 六、样例说明

`samples/trees/{deep,siblings,zero}.tree`、`samples/scripts/{deep,siblings,zero}.ops`、`samples/expected/{deep,siblings,zero}.out` 三组一一对应，脚本里的 `tree` 名字就是文件主名。

- `deep`：五层嵌套、其中一层额度为 0、祖先自身也在用额度；覆盖跨级借用、先借先还、到期收回、超量回收（UNDERFLOW）与借不到（INSUFFICIENT）。
- `siblings`：兄弟节点争抢同一个父节点的空闲额度，先申请者先用；覆盖借用、归还、到期收回后再次争抢与失败拒绝。
- `zero`：连续两层额度为 0，叶子只能越级向上借；覆盖自有与借用混合、到期收回与 `available=0` 的拒绝。

期望结果把每一步的 `STEP`、`STATE`、`DEBT` 全部写全，顺序敏感，可逐行核对；`samples/notes.md` 是配额被层锁死时的现场记录。

## 七、待补的文档

实现完成后在自己的 README 里补：运行方式与参数、目录结构、模拟流程与超时结算顺序、借用查找的复杂度、已知限制（不支持部分放行 / 转借 / 抢占），以及 HTML 报告的打开方式。
