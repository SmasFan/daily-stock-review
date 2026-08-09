---
description: 运行全部单元测试并报告结果。
agent: daily-review-dev
---

运行项目全部单元测试：

```
python3 -m unittest discover -s tests -v
```

要求：所有用例必须通过。若有失败，逐个修复后重跑，直到全绿。若测试文件缺失或新增了模块，先补充对应测试再跑。最后报告通过用例数。
