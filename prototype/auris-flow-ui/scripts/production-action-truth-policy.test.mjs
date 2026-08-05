import assert from "node:assert/strict";
import test from "node:test";

import {
  findTimedSuccessClaims,
  scanProductionActionTruth
} from "./production-action-truth-policy.mjs";

test("静态门禁识别计时器回调伪造写成功，但允许轮询退避", () => {
  assert.equal(
    findTimedSuccessClaims(
      `window.setTimeout(() => setNotice({ status: "success", title: "配置校验通过" }), 700);`
    ).length,
    1
  );
  assert.deepEqual(
    findTimedSuccessClaims(
      `await new Promise((resolve) => window.setTimeout(resolve, 700));`
    ),
    []
  );
});

test("生产写操作不存在计时器假成功，未配置能力 fail closed 且有可见原因", async () => {
  assert.deepEqual(await scanProductionActionTruth(), []);
});
