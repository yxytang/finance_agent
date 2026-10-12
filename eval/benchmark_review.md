# 100题人工复核清单

所有题目当前均为助手草案。需人工核对题意、参考要点、可替代证据、无答案边界和工具槽位。
政策题只按仓库快照评测；不能用此清单证明现行法规正确。

## f01-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f01
- 问题：专项附加扣除是不是直接减税？
- 参考要点：扣除减少应纳税所得额；实际省税与适用税率有关，不等于直接退回整个扣除额。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 起征点与应纳税所得额

> 专项附加扣除减少的是「应纳税所得额」，不是直接减税。

## f01-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f01
- 问题：申报了扣除，能不能把填的金额全数退给我？
- 参考要点：扣除减少应纳税所得额；实际省税与适用税率有关，不等于直接退回整个扣除额。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 起征点与应纳税所得额

> 专项附加扣除减少的是「应纳税所得额」，不是直接减税。

## f02-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f02
- 问题：综合所得的超额累进税率是什么意思？
- 参考要点：综合所得按年度计税，超额累进表示仅超出档位的部分适用更高税率。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 综合所得税率表

> 只有超出的那部分按更高税率

## f02-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f02
- 问题：涨薪跨了档，是不是所有收入都要按更高比例交？
- 参考要点：综合所得按年度计税，超额累进表示仅超出档位的部分适用更高税率。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 综合所得税率表

> 只有超出的那部分按更高税率

## f03-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f03
- 问题：累计预扣法为什么会让年底扣税更多？
- 参考要点：单位按当年累计收入计算累计应纳税额，再扣除已预缴税额；累计收入跨档会使后期扣税增加。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 累计预扣法

> 减去已经扣过的部分，得到这个月该扣多少。

## f03-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f03
- 问题：工资没涨，为何后几个月打到银行卡的钱反而少了？
- 参考要点：单位按当年累计收入计算累计应纳税额，再扣除已预缴税额；累计收入跨档会使后期扣税增加。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 累计预扣法

> 减去已经扣过的部分，得到这个月该扣多少。

## f04-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f04
- 问题：知识库中汇算清缴的办理时间是什么？
- 参考要点：文档给出的上一年度综合所得汇算期间为次年3月1日至6月30日；遗漏扣除可在汇算时补充。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 汇算清缴

> 每年 3 月 1 日到 6 月 30 日

## f04-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f04
- 问题：上一年的收入什么时候统一算一遍税？
- 参考要点：文档给出的上一年度综合所得汇算期间为次年3月1日至6月30日；遗漏扣除可在汇算时补充。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 汇算清缴

> 每年 3 月 1 日到 6 月 30 日

## f05-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f05
- 问题：子女教育扣除能由父母各扣一半吗？
- 参考要点：文档标准为每个子女每月2000元，可由一方100%或双方各50%扣除，一个纳税年度内不可变更。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 子女教育

> 每个子女每月 2000 元

## f05-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f05
- 问题：孩子上学的减税名额可以我们夫妻一起分吗？
- 参考要点：文档标准为每个子女每月2000元，可由一方100%或双方各50%扣除，一个纳税年度内不可变更。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 子女教育

> 每个子女每月 2000 元

## f06-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f06
- 问题：学历继续教育和职业资格继续教育扣除有何区别？
- 参考要点：学历继续教育每月400元、同一学历最长48个月；符合范围的职业资格继续教育在取证当年扣3600元。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 继续教育

> 取得证书的当年一次性扣 3600 元

## f06-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f06
- 问题：边工作边读书和考下职业证书，减税办法一样吗？
- 参考要点：学历继续教育每月400元、同一学历最长48个月；符合范围的职业资格继续教育在取证当年扣3600元。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 继续教育

> 取得证书的当年一次性扣 3600 元

## f07-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f07
- 问题：大病医疗扣除的门槛、上限和办理阶段是什么？
- 参考要点：医保目录范围内年度自付累计超过15000元的部分，在80000元限额内据实扣除，只能在汇算阶段办理。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 大病医疗

> 这条只能**在汇算清缴时**扣除

## f07-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f07
- 问题：一年自己掏的看病钱很多，达到多少才能减税，公司每月就能帮我扣吗？
- 参考要点：医保目录范围内年度自付累计超过15000元的部分，在80000元限额内据实扣除，只能在汇算阶段办理。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 大病医疗

> 这条只能**在汇算清缴时**扣除

## f08-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f08
- 问题：住房贷款利息专项扣除的标准和最长时间是多少？
- 参考要点：文档对符合条件的境内首套住房贷款利息按每月1000元定额扣除，最长240个月，夫妻可约定一方扣除。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 住房贷款利息

> 扣除期限最长不超过 240 个月

## f08-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f08
- 问题：第一套住房借款每月能少算多少应税收入，最多持续几年？
- 参考要点：文档对符合条件的境内首套住房贷款利息按每月1000元定额扣除，最长240个月，夫妻可约定一方扣除。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 住房贷款利息

> 扣除期限最长不超过 240 个月

## f09-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f09
- 问题：住房租金和住房贷款利息扣除能同时享受吗？
- 参考要点：两项只能二选一；夫妻在同一城市工作，租金扣除也只能由一方享受。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 住房租金

> 住房贷款利息和住房租金只能二选一

## f09-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f09
- 问题：我一边付房东钱一边供房，税务申报可以两头都填吗？
- 参考要点：两项只能二选一；夫妻在同一城市工作，租金扣除也只能由一方享受。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 住房租金

> 住房贷款利息和住房租金只能二选一

## f10-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f10
- 问题：非独生子女的赡养老人扣除如何分摊？
- 参考要点：非独生子女分摊每月3000元，每人不超过1500元，分摊需书面协议；文档适用对象包括年满60岁的父母。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 赡养老人

> 每人分摊的额度不超过每月 1500 元

## f10-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f10
- 问题：我有兄弟姐妹，照顾爸妈的减税额度可以全算给我吗？
- 参考要点：非独生子女分摊每月3000元，每人不超过1500元，分摊需书面协议；文档适用对象包括年满60岁的父母。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 赡养老人

> 每人分摊的额度不超过每月 1500 元

## f11-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f11
- 问题：五险一金里的工伤和生育保险需要个人缴费吗？
- 参考要点：文档说明工伤和生育保险由单位承担，个人不缴费；其他项目按当地规则缴纳，表中比例只是常见水平。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 谁交多少

> 工伤保险和生育保险**个人不缴费**

## f11-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f11
- 问题：工资条上每种保障都该从我工资里扣钱吗？
- 参考要点：文档说明工伤和生育保险由单位承担，个人不缴费；其他项目按当地规则缴纳，表中比例只是常见水平。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 谁交多少

> 工伤保险和生育保险**个人不缴费**

## f12-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f12
- 问题：社保缴费基数的上下限如何影响扣款？
- 参考要点：缴费基数受当地社会平均工资上下限约束；低于下限按下限、高于上限按上限，不一定等于实际工资。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 缴费基数有上下限

> 工资低于下限的，按下限交

## f12-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f12
- 问题：薪水翻倍了，为何保障缴款没跟着翻倍？
- 参考要点：缴费基数受当地社会平均工资上下限约束；低于下限按下限、高于上限按上限，不一定等于实际工资。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 缴费基数有上下限

> 工资低于下限的，按下限交

## f13-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f13
- 问题：住房公积金个人和单位缴纳的钱归谁？
- 参考要点：个人和单位缴纳的公积金都进入个人账户，可依条件用于买房、租房、还贷或提取。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 住房公积金值得单独说

> 个人交的和单位交的**都进你的个人账户**

## f13-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f13
- 问题：公司替我存的那笔住房钱，是公司的还是我自己的？
- 参考要点：个人和单位缴纳的公积金都进入个人账户，可依条件用于买房、租房、还贷或提取。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 住房公积金值得单独说

> 个人交的和单位交的**都进你的个人账户**

## f14-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f14
- 问题：社保和公积金断缴后影响一样吗？
- 参考要点：文档分别讨论医保待遇及连续年限、公积金贷款连续缴纳要求和养老累计年限；养老并非全部清零，各地规则有差异。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 断缴的后果

> 是累计计算的，但会减少缴费年限

## f14-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f14
- 问题：跳槽中间空了几个月，以前攒的保障年限都清零了吗？
- 参考要点：文档分别讨论医保待遇及连续年限、公积金贷款连续缴纳要求和养老累计年限；养老并非全部清零，各地规则有差异。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 断缴的后果

> 是累计计算的，但会减少缴费年限

## f15-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f15
- 问题：等额本金和等额本息的月供、总利息有何区别？
- 参考要点：等额本息月供固定、前期现金压力较小；等额本金本金固定、月供递减，前期压力较大，同条件下总利息较少。
- 必需工具槽位：[["search_knowledge"]]

证据：房贷.md → 等额本息 vs 等额本金

> 每月还的本金固定，利息逐月减少

## f15-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f15
- 问题：每月固定还同样的钱，与开始多还后来少还，哪种压力更大？
- 参考要点：等额本息月供固定、前期现金压力较小；等额本金本金固定、月供递减，前期压力较大，同条件下总利息较少。
- 必需工具槽位：[["search_knowledge"]]

证据：房贷.md → 等额本息 vs 等额本金

> 每月还的本金固定，利息逐月减少

## f16-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f16
- 问题：提前还房贷时为什么不能只比较预期投资收益率？
- 参考要点：比较贷款成本与资金收益时还要考虑收益的确定性；不能把有亏损可能的预期收益当作稳定收益。
- 必需工具槽位：[["search_knowledge"]]

证据：房贷.md → 提前还款划算吗

> 注意「稳拿」两个字

## f16-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f16
- 问题：借款成本确定，但投资可能亏钱，拿预估回报比较够不够？
- 参考要点：比较贷款成本与资金收益时还要考虑收益的确定性；不能把有亏损可能的预期收益当作稳定收益。
- 必需工具槽位：[["search_knowledge"]]

证据：房贷.md → 提前还款划算吗

> 注意「稳拿」两个字

## f17-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f17
- 问题：征信中的逾期记录五年从哪一天算起？
- 参考要点：按文档，逾期记录的五年保留期从结清欠款起算，不是从首次逾期起算。
- 必需工具槽位：[["search_knowledge"]]

证据：征信.md → 报告里有什么

> 从结清欠款那天算起，不是从逾期那天算起。

## f17-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f17
- 问题：那笔欠款晚付过，污点的等待时间是迟付当天开始还是补清当天开始？
- 参考要点：按文档，逾期记录的五年保留期从结清欠款起算，不是从首次逾期起算。
- 必需工具槽位：[["search_knowledge"]]

证据：征信.md → 报告里有什么

> 从结清欠款那天算起，不是从逾期那天算起。

## f18-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f18
- 问题：知识库中的LPR加点房贷利率由哪两项组成？
- 参考要点：文档用LPR加点解释房贷利率结构：LPR与合同约定加点共同构成；具体合同及调整规则需另行核实。
- 必需工具槽位：[["search_knowledge"]]

证据：房贷.md → 利率是怎么定的

> LPR（贷款市场报价利率）加点的形式

## f18-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f18
- 问题：住房借款报价里说基准再加一点，这个结构怎么理解？
- 参考要点：文档用LPR加点解释房贷利率结构：LPR与合同约定加点共同构成；具体合同及调整规则需另行核实。
- 必需工具槽位：[["search_knowledge"]]

证据：房贷.md → 利率是怎么定的

> LPR（贷款市场报价利率）加点的形式

## f19-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f19
- 问题：年终奖可以比较哪两种计税方式？
- 参考要点：文档介绍全年一次性奖金单独计税与并入综合所得两种方式，需比较结果并核实政策适用条件。
- 必需工具槽位：[["search_knowledge"]]

证据：年终奖计税.md → 两种算法，自己选

> 并入综合所得

## f19-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f19
- 问题：公司年底多发的钱，是和工资一起算还是另外算？
- 参考要点：文档介绍全年一次性奖金单独计税与并入综合所得两种方式，需比较结果并核实政策适用条件。
- 必需工具槽位：[["search_knowledge"]]

证据：年终奖计税.md → 两种算法，自己选

> 并入综合所得

## f20-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f20
- 问题：年终奖单独计税为何存在临界点？
- 参考要点：单独计税用奖金除以12确定档位，再以整笔奖金计算；跨越临界档位可能造成税额突增，不能只看税前奖金。
- 必需工具槽位：[["search_knowledge"]]

证据：年终奖计税.md → 那个著名的「临界点」

> 36,000

## f20-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f20
- 问题：年底奖励多发一块钱，为什么到手反而可能更少？
- 参考要点：单独计税用奖金除以12确定档位，再以整笔奖金计算；跨越临界档位可能造成税额突增，不能只看税前奖金。
- 必需工具槽位：[["search_knowledge"]]

证据：年终奖计税.md → 那个著名的「临界点」

> 36,000

## f21-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f21
- 问题：信用卡取现是否享有免息期？
- 参考要点：文档中的免息期面向消费，取现当天开始计息，不享受免息期；不同消费日期的免息时长也不同。
- 必需工具槽位：[["search_knowledge"]]

证据：信用卡.md → 免息期

> 取现从当天开始计息，没有免息期。

## f21-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f21
- 问题：刷卡买东西和直接把卡里的钱取出来，都可以过些天再付利息吗？
- 参考要点：文档中的免息期面向消费，取现当天开始计息，不享受免息期；不同消费日期的免息时长也不同。
- 必需工具槽位：[["search_knowledge"]]

证据：信用卡.md → 免息期

> 取现从当天开始计息，没有免息期。

## f22-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f22
- 问题：知识库信用卡最低还款例子中计息本金是什么？
- 参考要点：文档示例采用全额计息：10000元账单只还9000元时仍按10000元计息，并非仅剩余1000元；实际条款需核实。
- 必需工具槽位：[["search_knowledge"]]

证据：信用卡.md → 最低还款是个陷阱

> 利息不是按欠的 1000 算

## f22-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f22
- 问题：一万块刷卡钱还掉九千，文档说只对剩下一千收费吗？
- 参考要点：文档示例采用全额计息：10000元账单只还9000元时仍按10000元计息，并非仅剩余1000元；实际条款需核实。
- 必需工具槽位：[["search_knowledge"]]

证据：信用卡.md → 最低还款是个陷阱

> 利息不是按欠的 1000 算

## f23-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f23
- 问题：信用卡分期月费率为什么不能直接乘12当年化？
- 参考要点：手续费按原始本金收取而占用本金递减，直接乘12低估资金成本；应以现金流IRR等方法计算，文档换算只是近似。
- 必需工具槽位：[["search_knowledge"]]

证据：信用卡.md → 分期的真实利率

> 欠银行的钱在逐月减少，而手续费一直按最初的本金收

## f23-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f23
- 问题：钱越还越少但每月费用没变，报价的比例是不是低估了真实成本？
- 参考要点：手续费按原始本金收取而占用本金递减，直接乘12低估资金成本；应以现金流IRR等方法计算，文档换算只是近似。
- 必需工具槽位：[["search_knowledge"]]

证据：信用卡.md → 分期的真实利率

> 欠银行的钱在逐月减少，而手续费一直按最初的本金收

## f24-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f24
- 问题：日息万五按单利换成年化是多少？
- 参考要点：按单利口径0.05%乘365为18.25%；若利息滚入本金产生复利，实际成本还会更高。
- 必需工具槽位：[["search_knowledge"]]

证据：消费贷与真实利率.md → 「日息万五」是多少

> 0.05% × 365 = 18.25%

## f24-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f24
- 问题：借一万元每天五块钱的广告，全年比例该怎么算？
- 参考要点：按单利口径0.05%乘365为18.25%；若利息滚入本金产生复利，实际成本还会更高。
- 必需工具槽位：[["search_knowledge"]]

证据：消费贷与真实利率.md → 「日息万五」是多少

> 0.05% × 365 = 18.25%

## f25-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f25
- 问题：等本等息与等额本息的计息本金有什么不同？
- 参考要点：等本等息按原始本金计息、每期本金和利息固定；等额本息利息按剩余本金计，二者不可混淆。
- 必需工具槽位：[["search_knowledge"]]

证据：消费贷与真实利率.md → 「等本等息」是最坑的写法

> 等额本息是利息按剩余本金算

## f25-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f25
- 问题：两种名字很像的借款方案，一种每期费用不变，它和正常月供算法一样吗？
- 参考要点：等本等息按原始本金计息、每期本金和利息固定；等额本息利息按剩余本金计，二者不可混淆。
- 必需工具槽位：[["search_knowledge"]]

证据：消费贷与真实利率.md → 「等本等息」是最坑的写法

> 等额本息是利息按剩余本金算

## f26-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f26
- 问题：应急资金按全部消费还是必需开支计算？
- 参考要点：按必需开支而非全部消费估算，结合收入稳定性、房贷、家庭健康及再就业时间确定覆盖月数。
- 必需工具槽位：[["search_knowledge"]]

证据：应急资金.md → 要存多少

> 不是全部开支

## f26-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f26
- 问题：为失业留备用钱时，度假和娱乐的钱也要照常预留进去吗？
- 参考要点：按必需开支而非全部消费估算，结合收入稳定性、房贷、家庭健康及再就业时间确定覆盖月数。
- 必需工具槽位：[["search_knowledge"]]

证据：应急资金.md → 要存多少

> 不是全部开支

## f27-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f27
- 问题：应急资金的流动性、本金和收益优先级如何排列？
- 参考要点：文档将随时可取、本金安全放在收益率之前；存放时关注赎回限制，不能为更高收益牺牲应急可用性。
- 必需工具槽位：[["search_knowledge"]]

证据：应急资金.md → 放在哪里

> 随时能取

## f27-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f27
- 问题：备用钱为了多赚一点锁几年合适吗？
- 参考要点：文档将随时可取、本金安全放在收益率之前；存放时关注赎回限制，不能为更高收益牺牲应急可用性。
- 必需工具槽位：[["search_knowledge"]]

证据：应急资金.md → 放在哪里

> 随时能取

## f28-literal · dev · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f28
- 问题：家庭保险为什么先保障经济支柱？
- 参考要点：优先保障经济支柱，避免收入中断后房贷、教育和生活支出无法维持；文档分别说明寿险、重疾、医疗及意外的用途。
- 必需工具槽位：[["search_knowledge"]]

证据：保险.md → 顺序

> 先给家庭经济支柱买，其次才是孩子和老人。

## f28-paraphrased · dev · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f28
- 问题：全家的钱主要由一个人挣，优先给谁做好保障？
- 参考要点：优先保障经济支柱，避免收入中断后房贷、教育和生活支出无法维持；文档分别说明寿险、重疾、医疗及意外的用途。
- 必需工具槽位：[["search_knowledge"]]

证据：保险.md → 顺序

> 先给家庭经济支柱买，其次才是孩子和老人。

## f29-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f29
- 问题：R2理财是不是保本产品？
- 参考要点：风险等级低不等于保本，R2仍有亏损可能；应查看产品风险、资产及条款，而非把等级当作本金承诺。
- 必需工具槽位：[["search_knowledge"]]

证据：存款与理财.md → 风险等级

> R2

## f29-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f29
- 问题：银行说风险偏低，是否就承诺本金绝不会少？
- 参考要点：风险等级低不等于保本，R2仍有亏损可能；应查看产品风险、资产及条款，而非把等级当作本金承诺。
- 必需工具槽位：[["search_knowledge"]]

证据：存款与理财.md → 风险等级

> R2

## f30-literal · test · literal

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f30
- 问题：先存后花与信封法各自如何约束预算？
- 参考要点：先存后花在发薪时先转存储蓄再消费；信封法为各类支出预设上限，花完即止；固定比例预算需结合实际生活成本。
- 必需工具槽位：[["search_knowledge"]]

证据：记账与预算.md → 几种常见方法

> 先存后花

## f30-paraphrased · test · paraphrased

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：f30
- 问题：工资一来先转走一部分，和每类支出事先封顶，有什么不同？
- 参考要点：先存后花在发薪时先转存储蓄再消费；信封法为各类支出预设上限，花完即止；固定比例预算需结合实际生活成本。
- 必需工具槽位：[["search_knowledge"]]

证据：记账与预算.md → 几种常见方法

> 先存后花

## multi-01 · dev · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-01-07
- 问题：专项扣除是否直接退钱，大病医疗又在哪个阶段办理？
- 参考要点：扣除减少应纳税所得额；实际省税与适用税率有关，不等于直接退回整个扣除额。医保目录范围内年度自付累计超过15000元的部分，在80000元限额内据实扣除，只能在汇算阶段办理。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 起征点与应纳税所得额

> 专项附加扣除减少的是「应纳税所得额」，不是直接减税。

证据：专项附加扣除.md → 大病医疗

> 这条只能**在汇算清缴时**扣除

## multi-02 · dev · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-02-03
- 问题：综合所得超额累进和累计预扣如何共同影响年底工资？
- 参考要点：综合所得按年度计税，超额累进表示仅超出档位的部分适用更高税率。单位按当年累计收入计算累计应纳税额，再扣除已预缴税额；累计收入跨档会使后期扣税增加。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 综合所得税率表

> 只有超出的那部分按更高税率

证据：个税基础.md → 累计预扣法

> 减去已经扣过的部分，得到这个月该扣多少。

## multi-03 · dev · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-06-08
- 问题：继续教育和首套住房贷款利息的扣除标准、期限分别是什么？
- 参考要点：学历继续教育每月400元、同一学历最长48个月；符合范围的职业资格继续教育在取证当年扣3600元。文档对符合条件的境内首套住房贷款利息按每月1000元定额扣除，最长240个月，夫妻可约定一方扣除。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 继续教育

> 取得证书的当年一次性扣 3600 元

证据：专项附加扣除.md → 住房贷款利息

> 扣除期限最长不超过 240 个月

## multi-04 · dev · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-11-12
- 问题：工伤、生育是否从个人工资扣款，社保缴费基数是否等于实际工资？
- 参考要点：文档说明工伤和生育保险由单位承担，个人不缴费；其他项目按当地规则缴纳，表中比例只是常见水平。缴费基数受当地社会平均工资上下限约束；低于下限按下限、高于上限按上限，不一定等于实际工资。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 谁交多少

> 工伤保险和生育保险**个人不缴费**

证据：五险一金.md → 缴费基数有上下限

> 工资低于下限的，按下限交

## multi-05 · dev · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-13-18
- 问题：住房公积金的钱归谁，LPR加点的房贷利率又如何组成？
- 参考要点：个人和单位缴纳的公积金都进入个人账户，可依条件用于买房、租房、还贷或提取。文档用LPR加点解释房贷利率结构：LPR与合同约定加点共同构成；具体合同及调整规则需另行核实。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 住房公积金值得单独说

> 个人交的和单位交的**都进你的个人账户**

证据：房贷.md → 利率是怎么定的

> LPR（贷款市场报价利率）加点的形式

## multi-06 · dev · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-16-27
- 问题：手头的钱用于提前还房贷或作为应急资金，分别要考虑哪些收益和流动性问题？
- 参考要点：比较贷款成本与资金收益时还要考虑收益的确定性；不能把有亏损可能的预期收益当作稳定收益。文档将随时可取、本金安全放在收益率之前；存放时关注赎回限制，不能为更高收益牺牲应急可用性。
- 必需工具槽位：[["search_knowledge"]]

证据：房贷.md → 提前还款划算吗

> 注意「稳拿」两个字

证据：应急资金.md → 放在哪里

> 随时能取

## multi-07 · dev · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-17-22
- 问题：信用卡只还部分账单的计息本金是什么，逾期后征信保留期从哪天起算？
- 参考要点：按文档，逾期记录的五年保留期从结清欠款起算，不是从首次逾期起算。文档示例采用全额计息：10000元账单只还9000元时仍按10000元计息，并非仅剩余1000元；实际条款需核实。
- 必需工具槽位：[["search_knowledge"]]

证据：征信.md → 报告里有什么

> 从结清欠款那天算起，不是从逾期那天算起。

证据：信用卡.md → 最低还款是个陷阱

> 利息不是按欠的 1000 算

## multi-08 · dev · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-21-23
- 问题：信用卡取现免息吗，分期费用的真实年化又为什么高于报价？
- 参考要点：文档中的免息期面向消费，取现当天开始计息，不享受免息期；不同消费日期的免息时长也不同。手续费按原始本金收取而占用本金递减，直接乘12低估资金成本；应以现金流IRR等方法计算，文档换算只是近似。
- 必需工具槽位：[["search_knowledge"]]

证据：信用卡.md → 免息期

> 取现从当天开始计息，没有免息期。

证据：信用卡.md → 分期的真实利率

> 欠银行的钱在逐月减少，而手续费一直按最初的本金收

## multi-09 · dev · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-26-28
- 问题：家庭经济支柱保障和应急资金覆盖月数，分别应如何确定？
- 参考要点：按必需开支而非全部消费估算，结合收入稳定性、房贷、家庭健康及再就业时间确定覆盖月数。优先保障经济支柱，避免收入中断后房贷、教育和生活支出无法维持；文档分别说明寿险、重疾、医疗及意外的用途。
- 必需工具槽位：[["search_knowledge"]]

证据：应急资金.md → 要存多少

> 不是全部开支

证据：保险.md → 顺序

> 先给家庭经济支柱买，其次才是孩子和老人。

## multi-10 · test · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-04-05
- 问题：子女教育扣除能怎样分配，漏填后可在哪个时间段办汇算？
- 参考要点：文档给出的上一年度综合所得汇算期间为次年3月1日至6月30日；遗漏扣除可在汇算时补充。文档标准为每个子女每月2000元，可由一方100%或双方各50%扣除，一个纳税年度内不可变更。
- 必需工具槽位：[["search_knowledge"]]

证据：个税基础.md → 汇算清缴

> 每年 3 月 1 日到 6 月 30 日

证据：专项附加扣除.md → 子女教育

> 每个子女每月 2000 元

## multi-11 · test · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-09-10
- 问题：夫妻租金扣除和兄弟姐妹赡养老人扣除能否随意全填给一个人？
- 参考要点：两项只能二选一；夫妻在同一城市工作，租金扣除也只能由一方享受。非独生子女分摊每月3000元，每人不超过1500元，分摊需书面协议；文档适用对象包括年满60岁的父母。
- 必需工具槽位：[["search_knowledge"]]

证据：专项附加扣除.md → 住房租金

> 住房贷款利息和住房租金只能二选一

证据：专项附加扣除.md → 赡养老人

> 每人分摊的额度不超过每月 1500 元

## multi-12 · test · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-14-15
- 问题：换工作断缴保障有哪些影响，等额本金为何前期月供较高？
- 参考要点：文档分别讨论医保待遇及连续年限、公积金贷款连续缴纳要求和养老累计年限；养老并非全部清零，各地规则有差异。等额本息月供固定、前期现金压力较小；等额本金本金固定、月供递减，前期压力较大，同条件下总利息较少。
- 必需工具槽位：[["search_knowledge"]]

证据：五险一金.md → 断缴的后果

> 是累计计算的，但会减少缴费年限

证据：房贷.md → 等额本息 vs 等额本金

> 每月还的本金固定，利息逐月减少

## multi-13 · test · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-19-20
- 问题：年终奖有哪些计税方案，单独计税为何可能多发反而少拿？
- 参考要点：文档介绍全年一次性奖金单独计税与并入综合所得两种方式，需比较结果并核实政策适用条件。单独计税用奖金除以12确定档位，再以整笔奖金计算；跨越临界档位可能造成税额突增，不能只看税前奖金。
- 必需工具槽位：[["search_knowledge"]]

证据：年终奖计税.md → 两种算法，自己选

> 并入综合所得

证据：年终奖计税.md → 那个著名的「临界点」

> 36,000

## multi-14 · test · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-24-25
- 问题：日息万五的单利年化是多少，等本等息和等额本息为何不同？
- 参考要点：按单利口径0.05%乘365为18.25%；若利息滚入本金产生复利，实际成本还会更高。等本等息按原始本金计息、每期本金和利息固定；等额本息利息按剩余本金计，二者不可混淆。
- 必需工具槽位：[["search_knowledge"]]

证据：消费贷与真实利率.md → 「日息万五」是多少

> 0.05% × 365 = 18.25%

证据：消费贷与真实利率.md → 「等本等息」是最坑的写法

> 等额本息是利息按剩余本金算

## multi-15 · test · multi_evidence

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：multi-29-30
- 问题：R2是否保本，先存后花又如何限制消费预算？
- 参考要点：风险等级低不等于保本，R2仍有亏损可能；应查看产品风险、资产及条款，而非把等级当作本金承诺。先存后花在发薪时先转存储蓄再消费；信封法为各类支出预设上限，花完即止；固定比例预算需结合实际生活成本。
- 必需工具槽位：[["search_knowledge"]]

证据：存款与理财.md → 风险等级

> R2

证据：记账与预算.md → 几种常见方法

> 先存后花

## unsupported-01 · dev · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-01
- 问题：知识库能确定今天公布的最新LPR数值吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-02 · dev · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-02
- 问题：知识库能给出2026年北京社保缴费基数的精确上下限金额吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-03 · dev · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-03
- 问题：知识库能确定我这家银行今天执行的信用卡取现费率吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-04 · test · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-04
- 问题：文档能确定全年一次性奖金单独计税政策具体截止哪一年吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-05 · test · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-05
- 问题：知识库能给出所有城市今天适用的医保断缴宽限期吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-06 · dev · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-06
- 问题：请根据知识库告诉我某只股票明天的准确收盘价。
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-07 · dev · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-07
- 问题：知识库有没有比特币下周一定上涨的证明？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-08 · dev · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-08
- 问题：请仅依据知识库比较两份没有提供的具体保险合同，选出唯一赢家。
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-09 · test · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-09
- 问题：知识库能确定我本人实际信用评分和可获批额度吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-10 · test · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-10
- 问题：请根据知识库说出我未上传的银行卡当前余额。
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-11 · dev · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-11
- 问题：知识库能确定某银行今天哪款定期存款利率最高吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-12 · dev · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-12
- 问题：仅凭这些文档能证明指定基金下个月不会亏吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-13 · dev · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-13
- 问题：知识库能列出某市今天办理公积金提取的完整材料清单吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-14 · test · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-14
- 问题：文档能确定所有银行提前还房贷违约金的统一百分比吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## unsupported-15 · test · unanswerable

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：unsupported-15
- 问题：知识库能确认我未提供合同的保险一定赔付这次住院吗？
- 参考要点：现有知识库无法确定该具体事实。明确说明证据不足，指出需要最新官方资料、账户信息或具体合同；不得编造确定数值或保证。
- 必需工具槽位：[["search_knowledge"]]

## routing-01 · dev · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-01
- 问题：查一下我的账单中2026年8月餐饮外卖花了多少。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。
- 必需工具槽位：[["query_transactions"]]

## routing-02 · dev · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-02
- 问题：查我的账单，按月份汇总2026年的支出。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。
- 必需工具槽位：[["query_transactions"]]

## routing-03 · dev · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-03
- 问题：找一下我的账单里的异常大额交易。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。
- 必需工具槽位：[["find_anomalies"]]

## routing-04 · dev · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-04
- 问题：根据我的账单列出重复订阅支出。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。
- 必需工具槽位：[["list_subscriptions"]]

## routing-05 · test · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-05
- 问题：根据当前账单推算本月到月底大概会花多少。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。
- 必需工具槽位：[["project_spending"]]

## routing-06 · test · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-06
- 问题：看看我的账单数据覆盖哪些月份、有多少条记录。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。
- 必需工具槽位：[["describe_data"]]

## routing-07 · dev · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-07
- 问题：查询2026年8月餐饮账单，再根据知识库解释累计预扣法。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。单位按当年累计收入计算累计应纳税额，再扣除已预缴税额；累计收入跨档会使后期扣税增加。
- 必需工具槽位：[["query_transactions"], ["search_knowledge"]]

证据：个税基础.md → 累计预扣法

> 减去已经扣过的部分，得到这个月该扣多少。

## routing-08 · dev · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-08
- 问题：找出我的账单异常交易，再根据知识库解释应急资金要存多少。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。按必需开支而非全部消费估算，结合收入稳定性、房贷、家庭健康及再就业时间确定覆盖月数。
- 必需工具槽位：[["find_anomalies"], ["search_knowledge"]]

证据：应急资金.md → 要存多少

> 不是全部开支

## routing-09 · test · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-09
- 问题：查2026年8月总支出，再依据知识库介绍先存后花。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。先存后花在发薪时先转存储蓄再消费；信封法为各类支出预设上限，花完即止；固定比例预算需结合实际生活成本。
- 必需工具槽位：[["query_transactions"], ["search_knowledge"]]

证据：记账与预算.md → 几种常见方法

> 先存后花

## routing-10 · test · tool_routing

- [ ] 人工复核（当前：assistant_draft_pending_human_review）
- 题族：routing-10
- 问题：推算本月到月底的支出，再依据知识库解释R2理财是否保本。
- 参考要点：账单数值必须来自对应工具，不能从知识文档推断。风险等级低不等于保本，R2仍有亏损可能；应查看产品风险、资产及条款，而非把等级当作本金承诺。
- 必需工具槽位：[["project_spending"], ["search_knowledge"]]

证据：存款与理财.md → 风险等级

> R2
