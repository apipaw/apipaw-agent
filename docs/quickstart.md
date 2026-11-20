## 🚀 What's New | 本次重磅更新

**[EN]** **The local panel is now hardened against the attacks that could silently steal your node.** The panel listens on your own machine only — but "only on my machine" is not the same as "only I can reach it". Three doors are now locked. (1) **Host check**: a request whose `Host` header is not `127.0.0.1` / `localhost` is rejected with **400**, which shuts down *DNS rebinding* — a trick where a web page points a domain at your loopback address and your browser hands it your node credential. (2) **Origin check on every write**: the endpoints that change your settings (`/api/bind`, `/api/prefs`, `/api/mesh/prefs`, the model pull / evolve endpoints) now accept a request only when it comes from the panel's own page; a request carrying a foreign `Origin`, or the browser's `Sec-Fetch-Site: cross-site` marker, is refused with **403**. A plain command line (`curl`) has no `Origin` and keeps working exactly as before. (3) **No more silent one-click binding**: a link that carries `node_id` / `node_secret` now **only pre-fills the boxes** and shows a banner with a **「立即保存绑定」** button — it no longer binds your node behind your back, so a web page you merely visited can no longer re-bind your machine. On top of that, **backup gateway addresses** must now be `wss://` / `https://` and point at a trusted domain (`*.apipaw.com` by default, extendable with `APIPAW_TRUSTED_GATEWAY_DOMAINS`), so a tampered configuration cannot redirect your node's traffic to an attacker's server. **Nothing about daily use changes**: the panel, the bind button and the model management all work exactly as before.

**[CN]** **本地面板现在把「能偷走您节点」的几条路都堵上了。** 面板只监听本机，但**「只监听本机」不等于「只有本人能访问」**，这次上了三道闸门：(1) **Host 校验** —— `Host` 头不是 `127.0.0.1` / `localhost` 的请求一律 **400** 拒绝，这就堵住了 *DNS Rebinding*（把域名解析到回环地址、再借您的浏览器来偷凭证的那类招数）。(2) **写端点 Origin 校验** —— 所有会改配置的接口（`/api/bind`、`/api/prefs`、`/api/mesh/prefs` 以及模型拉取 / 进化接口）现在只接受来自面板自身的请求；带着外部 `Origin` 或浏览器 `Sec-Fetch-Site: cross-site` 标记的请求一律 **403** 拒绝。而命令行（`curl`）本就不带 `Origin`，照常可用，不影响老习惯。(3) **不再「一键自动绑定」** —— 带 `node_id` / `node_secret` 的链接现在**只负责预填输入框**并弹出提示条与 **「立即保存绑定」** 按钮，**绝不**在您背后把节点绑走；于是「只是点开某个网页」再也不会悄悄改掉您机器的归属。另外，**备用网关地址**现在必须是 `wss://` / `https://`，且域名必须命中信任域（默认 `*.apipaw.com`，可用 `APIPAW_TRUSTED_GATEWAY_DOMAINS` 扩展），被篡改的配置再也不能把节点流量导向别人的服务器。**日常使用完全不受影响**：面板、绑定按钮、模型管理一切照旧。

**[EN]** **The local panel finally behaves at 100 % zoom — and "one-click fill" really binds.** Three fixes that came straight from real-machine reports: (1) the page was vertically centred, so once the panel grew past the window height its **top half was pushed off-screen** — the title, the **Node ID / Node Secret** boxes and the **「绑 定 节 点」** button were simply *unreachable*, no matter how far you scrolled. The layout now starts at the top and scrolls properly, so the first thing you see is always the form. (2) The console's "fill this machine" link (`?node_id=…&node_secret=…`) filled in the **ID but not the secret**, and the periodic status poll then wrote the old `node_default_001` back over your input — which is why clicking bind complained *"请填写 Node Secret"*. Now **both** fields are filled, the poll **never overwrites** what you are editing, and a banner with a **「立即保存绑定」** button appears right under the title; when the link carries a complete credential the panel **pre-fills it and waits for you to click「立即保存绑定」**（v1.0.20 起不再自动绑定）, and wipes the secret from the address bar. (3) The cloud's "recommended models" and "golden memory" panels no longer die with `403 Forbidden` — the agent now sends a proper browser-style **User-Agent** (Cloudflare was blocking the default `Python-urllib` one), uses a **4-second timeout** with automatic retries, and keeps the last good answer in memory so a hiccup no longer empties the panel.

**[CN]** **本地面板在 100% 缩放下终于正常了 —— 「一键填入」也真的能绑上。** 三个来自真机反馈的修复：(1) 页面原本是**垂直居中**排版的，一旦面板高度超过窗口高度，卡片的**上半截就被顶到屏幕之外** —— 标题、**Node ID / Node Secret** 输入框和 **「绑 定 节 点」** 按钮全都**看不见也点不到**，滚到最顶也没用。现在改为从顶部开始排版并允许正常滚动，**打开第一眼永远是表单本身**。(2) 控制台的「一键填入本机 Agent」链接（`?node_id=…&node_secret=…`）以前只填了 **ID、没填密钥**，随后 **5 秒一次的 `/api/status` 轮询**又把旧值 `node_default_001` 写回输入框 —— 于是点「绑定节点」就报 **「请填写 Node Secret」**。现在**两个框都会填好**，轮询**绝不再覆盖**你正在编辑或刚预填的内容，标题下方还会出现一条提示横幅和 **「立即保存绑定」** 快捷按钮；链接里凭证齐全时，面板会**预填好并等您点「立即保存绑定」**（v1.0.20 起不再自动绑定），同时顺手把地址栏里的明文密钥抹掉。(3) 「云端推荐列表」与「黄金记忆」两块面板不再报 `403 Forbidden` —— Agent 现在会带上合规的浏览器 **User-Agent**（此前默认的 `Python-urllib` 被 Cloudflare 边缘拦了），超时收紧到 **4 秒**并自动重试，还会**记住上一次成功的结果**兜底，网络抖动时面板不再整块变空。

**[EN]** **Two small machines in your living room now add up to one big machine.** The new **Pipeline Mesh** probes the other nodes on your local network, measures their round-trip time and asks how much VRAM they have — and only merges with peers that answer (a peer over **25 ms** away, unreachable, or silent about its VRAM is dropped, never guessed at). Two 6 GB nodes therefore present themselves to the cloud as a **12 GB** cluster, and an 80-layer model is split across them as **L0-39 / L40-79**. The cloud uses the **larger** of "this machine's VRAM" and "the cluster's VRAM" when it checks whether your node may serve a demanding model, so a 12 GB model now reaches a pair of 6 GB boxes instead of nobody. Everything is configured in the local panel (**http://127.0.0.1:52415/ui**) under **「🔗 Pipeline Mesh 局域网多机显存合体」**, and the cloud publishes the resulting topology for everyone to see.

**[CN]** **客厅里的两台小机器，现在能当成一台大机器用。** 新增的 **Pipeline Mesh（局域网张量切片合体）** 会探测局域网内的其它节点，测它们的往返延迟、问清楚它们有多少显存 —— 只与**答得上话**的节点合体：往返超过 **25 ms**、连不上、或者**没上报显存**的节点一律剔除，绝不把「未知」当成「有」。于是两台 6GB 节点向云端呈现为 **12GB** 集群，80 层模型被切成 **L0-39 / L40-79** 两片。云端判断「这台机器能不能接大模型」时取**单机显存与集群显存的较大者**，于是 12GB 门槛的模型终于能派到两台 6GB 小机器头上，而不是谁都接不了。全部配置在本地面板（**http://127.0.0.1:52415/ui**）的 **「🔗 Pipeline Mesh 局域网多机显存合体」** 区块里完成，合出来的拓扑会公开公示。

**[EN]** **Macs are first-class now.** On macOS the node reads the system's own **`HIDIdleTime`** counter (`ioreg -c IOHIDSystem`, nanoseconds) to decide when you touched the keyboard — so the **50 ms yield** guarantee ("get out of the way the moment the human starts typing") holds on Apple Silicon exactly as it does on Windows. The same run detects the chip (`sysctl -n machdep.cpu.brand_string`) and the unified memory (`sysctl -n hw.memsize`), and reports the unified memory as usable VRAM at **75 %** — an M-series box with 16 GB is treated as a 12 GB node. On a Mac with no separate GPU the temperature reading simply shows as unavailable; nothing breaks and nothing is faked.

**[CN]** **Mac 现在也是一等公民。** 在 macOS 上，节点直接读系统自带的 **`HIDIdleTime`** 计数器（`ioreg -c IOHIDSystem`，单位纳秒）来判断您有没有碰键盘 —— 于是「人一动手就立刻让退」的 **50 ms** 承诺，在苹果芯片上和 Windows 一样成立。同一轮探测还会识别芯片（`sysctl -n machdep.cpu.brand_string`）与统一内存（`sysctl -n hw.memsize`），并按 **75%** 折算成可用显存上报 —— 16GB 统一内存的 M 系列机器会被当作 12GB 节点。没有独立显卡的 Mac，温度读数只会显示为不可用，既不报错也不瞎猜。

**[EN]** **The node survives a gateway outage on its own.** Add backup gateway URLs in the panel (**「多网关主备容灾」**) and the node will automatically switch to a backup when the primary answers **502 / 503 / 504** (or the connection drops) — but only after three consecutive failures, so a single hiccup never moves you. **4xx never triggers a switch**: a request that was simply wrong stays on the primary, because the gateway is fine. Once the primary recovers, the node probes it and **switches back on its own**. The current gateway (primary or backup) is shown in the panel and travels with every heartbeat, so the console always knows which gateway a node is really talking to.

**[CN]** **网关挂了，节点自己会绕过去。** 在面板里填上备用网关地址（**「多网关主备容灾」**），主网关返回 **502 / 503 / 504**（或连接断开）时，节点会自动切到备用 —— 但要**连续失败三次**才切，一次抖动绝不会把您挪走。**4xx 绝不触发切换**：请求本身不对说明网关是好的，就留在主网关上。主网关恢复后，节点会主动探测并**自动切回**。当前生效的是主还是备，会显示在面板里、并随每次心跳上报，控制台因此永远知道这台节点究竟在跟哪个网关说话。

**[EN]** **Your node now signs its own answers — the cloud can finally tell real work from fakes.** Every finished inference is stamped with a **Proof-of-Inference receipt**: the node hashes the exact text it produced (`SHA-256`) and signs `request_id : model_id : prompt_tokens : completion_tokens : sha256(output)` with **HMAC-SHA256** using your node's own secret. The receipt travels with the answer frame and with the heartbeat, and the cloud re-computes the same signature with the same key — so **forged receipts**, **inflated token counts** (claiming 5 000 tokens for a 12-character answer) and **low-entropy garbage** can all be spotted and the offender's credit score can be docked. Nothing changes for you: the panel (**http://127.0.0.1:52415/ui**) works exactly as before, the secret never leaves your machine, and a node without a secret simply sends no receipt (treated as "no proof", never as fraud).

**[CN]** **节点现在会给自己的答案签名 —— 云端终于能分辨真干活与造假。** 每次推理结束都会打上一枚 **PoI（Proof-of-Inference）签名收据**：节点先对**自己产出的原文**做 `SHA-256`，再用**节点密钥**对 `request_id : model_id : prompt_tokens : completion_tokens : sha256(输出)` 做 **HMAC-SHA256** 签名。收据随回答帧与心跳一起上送，云端用同一把密钥重算同一签名即可验签 —— 于是**伪造收据**、**虚报 token 数**（12 个字的答案却报 5000 token）、**低熵垃圾输出**都能被识别，违规节点的信用分会被扣。**对您没有任何影响**：本地面板（**http://127.0.0.1:52415/ui**）照常使用，密钥绝不出您的机器；没有密钥的节点则直接不带上收据（云端按「无证明」处理，**绝不**当成欺诈）。

**[EN]** **Your GPU now reports its own temperature and fan speed.** The node reads `nvidia-smi` non-blockingly on every heartbeat and over the live WebSocket channel, so the cloud knows exactly how hot your card is running — the local panel (**http://127.0.0.1:52415/ui**) shows **「GPU 温度」** next to the node's other vitals, turning amber when it climbs and red when it crosses the 75 °C overheat line. Machines **without** an NVIDIA card are perfectly fine: the reading simply shows as unavailable instead of guessing, and nothing ever blocks or breaks the heartbeat. For demos you can force a value with `APIPAW_MOCK_GPU_TEMP_C` / `APIPAW_MOCK_GPU_FAN_PCT`.

**[CN]** **显卡自己会报温度和风扇转速了。** 节点在每次心跳与每条长连接上**非阻塞**读取 `nvidia-smi`，云端因此确切知道您的显卡跑到了多热 —— 本地面板（**http://127.0.0.1:52415/ui**）在节点状态一栏显示**「GPU 温度」**，温度升高时变琥珀色、越过 75 °C 过热线时变红。**没有 NVIDIA 独显**的机器完全不受影响：读数只会显示为「不可用」而不是瞎猜，心跳绝不会因此被阻塞或出错。做演示时可用 `APIPAW_MOCK_GPU_TEMP_C` / `APIPAW_MOCK_GPU_FAN_PCT` 强制注入数值。

**[EN]** **One-click model evolution now actually works end to end.** The `Modelfile` parser used to mistake ordinary Python-looking lines *inside* a multi-line `SYSTEM` / `MESSAGE` block for top-level directives — which silently truncated the golden memory and made Ollama reject the recipe with an "invalid model name" error. The parser now treats **only column-0 lines** as directives and reads multi-line blocks whole, so the cloud's curated memory arrives at your local engine intact. Two historical test warnings were also cleaned up (the usage-report and WebSocket keep-alive suites), and the whole agent test suite now runs green.

**[CN]** **「一键模型进化」现在真的跑得通了。** 以前解析 `Modelfile` 时，`SYSTEM` / `MESSAGE` **多行**正文里那些看起来像 Python 代码的行会被误当成顶层指令，导致黄金记忆被截断，Ollama 还会报 "invalid model name"。现在解析器只把**行首（第 0 列）**的行当指令，多行正文整段读取 ⇒ 云端策展的记忆完整无损地送进本机引擎。同时修好了两个历史遗留的测试告警（用量上报与 WebSocket 保活两套用例），Agent 全量单测现已全绿。

**[EN]** **Bake a cloud-trained model on your own machine — with one click, no GPU, no training.** The new **「🧬 云端黄金记忆本地模型进化」(One-Click Model Evolution)** block in the local panel (**http://127.0.0.1:52415/ui**) pulls the platform's **curated golden memory** (the best buyer-approved answers, already desensitized) from the cloud, receives it as a standard Ollama `Modelfile`, and asks your **local** Ollama to synthesise a derived model from it (e.g. `qwen2.5-apipaw-golden:latest`). The result is a model that already carries the cloud's best answers as few-shot guidance — **zero training cost, zero GPU**. Tick **「合成后立即设为当前接单模型」** and the node starts serving it on the next dispatch. If your local Ollama is not running, the panel says so in plain words instead of failing.

**[CN]** **在您自己的机器上，一键烘焙出带云端记忆的进化模型 —— 不用显卡、不用训练。** 本地面板（**http://127.0.0.1:52415/ui**）新增 **「🧬 云端黄金记忆本地模型进化」** 区块：从云端拉取**业主策展的黄金记忆**（已被买家采纳的优质问答，落库前已脱敏），以标准 Ollama `Modelfile` 的形式下发给本机，再让**本机** Ollama 合成一个派生模型（如 `qwen2.5-apipaw-golden:latest`）—— 相当于把云端最好的答案直接写进模型当成少样本范例，**零训练成本、零显卡**。勾上 **「合成后立即设为当前接单模型」**，下一次派单即用它接单。本机 Ollama 没启动时，面板会用人话如实告诉您，而不是直接报错。

**[EN]** **No more command line for models.** The local panel (**http://127.0.0.1:52415/ui**) gained a **「本地 Ollama 模型管理」(Local Ollama Models)** block: it lists the models already installed on this machine (and tells you plainly when Ollama is not running), shows what the platform currently recommends — models that online nodes are actually serving, with their price and how much VRAM they need — and then does the rest for you. **「一键拉取并绑定」(Pull & Bind)** downloads the model in the background and automatically writes it into `agent.config.json` as your serving model; **「设为当前接单模型」(Set as serving model)** switches an already-installed model with one click. Model names are strictly validated, and the node only ever talks to `127.0.0.1` — no credential ever leaves your machine.

**[CN]** **装模型不用再开命令行了。** 本地面板（**http://127.0.0.1:52415/ui**）新增 **「本地 Ollama 模型管理」** 区块：列出本机已装好的模型（Ollama 没启动也会如实告诉您），再把**云端当前推荐**的模型摆出来——也就是线上真有节点在跑的那些，带价格和显存门槛。剩下的动作面板替您做：**「一键拉取并绑定」** 会在后台把模型下载下来，并自动写进 `agent.config.json` 成为您的接单模型；**「设为当前接单模型」** 则让已装的模型一键切换。模型名经过严格校验，节点只跟 `127.0.0.1` 说话，任何凭证都不会离开您的机器。

**[EN]** The panel now **fills itself in from the link**: open `http://127.0.0.1:52415/ui?node_id=your-node&node_secret=your-secret` and the Node ID and Node Secret are already in the boxes — just click **"绑 定 节 点"**. That link is what the owner console hands out when it issues a credential in one click, so "get a credential → start the node" is now a copy-and-paste instead of a transcription job. The secret is wiped from the address bar as soon as it is read, so it does not linger in your browser history.

**[CN]** 面板现在**能从链接自动填好**：打开 `http://127.0.0.1:52415/ui?node_id=你的节点&node_secret=你的密钥`，Node ID 与 Node Secret 已经填在框里了，点「绑 定 节 点」即可。业主控制台一键签发凭证时给出的正是这条链接 —— 于是「拿到凭证 → 启动节点」从**手抄**变成了**复制粘贴**。密钥一读入即从地址栏抹掉，不会留在浏览器历史里。

**[EN]** A configuration file can no longer be silently overwritten by accident: the node's settings live in exactly one place (`agent.config.json`), and any tool that asks for a *different* settings file now gets its own private copy instead of quietly writing over your real one.

**[CN]** 配置文件不会再被**静默改写**：节点的配置只有一份（`agent.config.json`），任何工具想要**另一份**配置时，现在会拿到属于自己的独立副本，而不是悄悄把你真实的那份覆盖掉。

**[EN]** Your machine now **tells the cloud which models it can actually serve** — every heartbeat and every live connection carries the local model list plus the exact engine tag (an Ollama tag, or a vLLM / llama.cpp model name). The cloud merges that into the node registry **by itself**, so a freshly connected node shows up as *model-serving* right away. Until now you had to ask the platform to bind the models by hand for every single machine; that chore is gone. A node that reports an **empty** list is treated as "confirmed: no engine here" and is skipped for inference work, while a node that reports nothing at all keeps working as before.

**[CN]** 您的机器现在会**主动告诉云端自己到底能跑哪些模型**——每次心跳与每条长连接都会带上本地模型清单和引擎 tag（Ollama tag，或 vLLM / llama.cpp 的模型名）。云端据此**自动合并**进节点注册表，新接入的节点立刻就能被派发推理任务。此前每台新机器都要**手工**跑脚本绑模型，这件苦差事已经没有了。上报**空清单**的节点会被判定为「确认没有推理引擎」而不再参与派单；**完全不上报**的老节点行为不变，照常接单。

**[EN]** The local panel (**http://127.0.0.1:52415/ui**) gained two new controls, both saved to `agent.config.json` and **effective immediately** — no restart, no editing `start.bat`: a **「强制待命模式」(Force Idle)** checkbox that pins the node to *always available* regardless of mouse and keyboard activity (perfect for a dedicated mining box), and a **「本地推理模型」(Local Inference Model)** input where you type the engine tag your machine should use. If you still prefer the environment variable `APIPAW_FORCE_IDLE=1`, it keeps working exactly as before — the checkbox just makes it a one-click operation instead of a file-editing ritual.

**[CN]** 本地面板（**http://127.0.0.1:52415/ui**）新增两个开关，都写入 `agent.config.json` 且**立即生效**——不用重启、不用改 `start.bat`：**「强制待命模式」**勾选后无论键鼠是否有活动都把节点钉在「可接单」状态（适合专门挂机的机器）；**「本地推理模型」**输入框用来指定本机实际使用的引擎 tag。原先的环境变量 `APIPAW_FORCE_IDLE=1` 仍然完全可用——区别只是从「改脚本」变成「点一下」。

**[EN]** Binding a node no longer needs a server terminal: the owner console can now issue a **Node ID + Node Secret** for you in one click, and you paste those two values straight into the panel's bind box. Each machine gets its own credential, so revoking one machine does not disturb the others.

**[CN]** 绑定节点不必再开服务器终端：业主控制台现在可以**一键签发** `Node ID + Node Secret`，直接粘进面板的绑定框即可。每台机器一张独立凭证，吊销其中一台不会牵连其它机器。

**[EN]** The node can now talk to **OpenAI-compatible** inference servers — **vLLM** and **llama.cpp** are both supported, next to the Ollama it already knew. Point the node at your engine in `agent.config.json` (`backend_vllm_base_url` / `backend_llamacpp_base_url`, plus `backend_api_key` if your engine requires one). The auto-probe **only** tries an OpenAI-compatible engine when you write its address down explicitly — it will never mistake some unrelated HTTP service (a web app, or the APIPaw gateway itself, which also lives on port 8000) for an inference engine. Token counts are taken from the engine's own `usage` report, so what the cloud bills you matches what the engine really produced.

**[CN]** 节点现在支持对接 **OpenAI 兼容**推理服务——除原有的 Ollama 外，**vLLM** 与 **llama.cpp** 都能直接用。在 `agent.config.json` 里填好引擎地址（`backend_vllm_base_url` / `backend_llamacpp_base_url`，引擎要鉴权就再填 `backend_api_key`）即可。自动探测**只在您显式写了地址**时才会去试 OpenAI 兼容引擎——绝不会把某个不相干的 HTTP 服务（比如网页应用，或同样占用 8000 端口的 APIPaw 网关本身）误认成推理引擎。token 数取自引擎**自报**的 `usage`，云端计费与引擎真实产出对得上。

**[EN]** The node now measures its **own** speed and reports it: time-to-first-token and tokens-per-second travel with each answer, and the cloud uses those **real** numbers instead of guessing from network round-trips. Node owners can now see how fast their machine actually is, and the platform stops billing on a rough estimate.

**[CN]** 节点现在会**自测**并上报真实性能：首字延迟（TTFT）与吐字速度（tokens/s）随每次回答一并上报，云端据此采用**真实**性能口径，不再依赖网关观测到的网络往返时间去估算。节点业主能看到自己机器的真实速度，平台也不再拿粗略估算当依据。

**[EN]** If the inference engine **dies halfway through** an answer, the node now reports it as a **broken stream** instead of shipping half an answer as if it were complete — the cloud then **does not charge you** for that request. A failure that happens *before* the engine even starts is still reported separately as "engine unavailable", so the two stay distinguishable. The node never silently retries a failed request, and never sends a "done" marker for an answer that did not finish.

**[CN]** 推理引擎**答到一半挂掉**时，节点现在会明确上报「断流」，而不是把半截输出当成完整答案交上去——云端据此**不向您收费**。开跑前就失败仍单独报「引擎不可用」，两类故障**不混淆**。节点对失败的请求**不做静默重试**，没拿到完整答案时**绝不**回「完成」标记。

**[EN]** The node now answers with **exactly the model the cloud asked for**. If the cloud names a model this machine does not have, the node says so plainly and lists what it does have — it will never quietly answer with a different model and bill you for it. The node also reports its **model list** to the cloud, and the cloud only dispatches a task to a node that actually has that model loaded (older nodes that report nothing are still accepted, so existing nodes keep working). If this machine has exactly one model, that one may stand in.

**[CN]** 节点现在**严格按云端点名的模型**作答：云端点名而本机没有 ⇒ 明确报错并列出本机可用模型，**绝不**偷偷用别的模型顶替（否则您会为没点名的模型付费）；同时把本机**模型清单**上报云端，云端只把任务派给**确实装载了该模型**的节点（老版本节点未上报清单仍照常接单，存量节点不受影响）。本机只有一个模型时允许其顶替。

---

## 🛠️ Quick Start | 傻瓜式安装指南

**[EN] Windows Users:**
1. Download `{{ZIP_NAME}}` from the Assets below and extract it to a folder.
2. Double-click `install.bat` to automatically initialize the environment.
   - **In mainland China:** double-click `install_cn.bat` instead — it uses the Tsinghua TUNA pip mirror for much faster downloads.
   - **Overseas:** double-click `install.bat` — it uses the official PyPI index.
3. Double-click `start.bat` to run the agent. A Web UI will automatically pop up in your browser.
4. Enter your `Node ID` and `Node Secret` to bind the node. Done!

**[CN] Windows 用户：**
1. 下载下方 Assets 中的 `{{ZIP_NAME}}` 并将其解压到一个文件夹。
2. 双击运行安装脚本，等待环境自动初始化完成。
   - **如果您在中国大陆**，请双击运行 `install_cn.bat` 以开启国内网络加速（使用清华 TUNA 镜像源）；海外用户请双击 `install.bat`（使用官方 PyPI 源）。
3. 双击运行 `start.bat` 启动节点，浏览器将自动弹出 Web UI 绑定面板。
4. 填入系统分配给您的 `Node ID` 和 `Node Secret`，点击绑定即可亮起绿灯连接云端！

---

## 💻 Advanced Users | 进阶用户 (macOS / Linux)

**[EN]** You can still use the one-liner installation scripts.

**[CN]** 依然兼容历史版本的一键安装脚本，终端运行：

### Windows (PowerShell)
```powershell
irm https://raw.githubusercontent.com/apipaw/apipaw-agent/main/install.ps1 | iex
```

### macOS / Linux
```bash
curl -fsSL https://raw.githubusercontent.com/apipaw/apipaw-agent/main/install.sh | bash
```

---

## ✅ Verify | 验证安装

**[EN]** Check that the node is up and running:

**[CN]** 确认节点已启动并成功连接云端：

```bash
curl http://127.0.0.1:52415/health
```

---

## 🧠 推理引擎 | Real Inference Engine

**[EN]** The node probes a local **Ollama** server (`http://127.0.0.1:11434`) automatically. Configure it in `agent.config.json`:

```json
{
  "inference_backend": "auto",
  "backend_base_url": "http://127.0.0.1:11434",
  "backend_model": "",
  "backend_timeout_sec": 60
}
```

- `inference_backend`: `auto` (probe Ollama) / `ollama` / `vllm` / `llamacpp`
- `backend_base_url`: address of your inference engine
- `backend_model`: leave empty to use the model the cloud asks for
- `backend_timeout_sec`: engine timeout in seconds
- `backend_vllm_base_url` / `backend_llamacpp_base_url`: address of your **OpenAI-compatible** engine (vLLM / llama.cpp). Leave empty ⇒ **not** probed automatically, so an unrelated service on port 8000 can never be mistaken for an inference engine
- `backend_api_key`: optional API key for the OpenAI-compatible engine (`--api-key`)
- Status: `curl http://127.0.0.1:52415/health` → the `inference` field tells you which engine is live and which models it can serve

**[CN]** 节点会自动探测本机 **Ollama**（`http://127.0.0.1:11434`）。在 `agent.config.json` 里配置：

```json
{
  "inference_backend": "auto",
  "backend_base_url": "http://127.0.0.1:11434",
  "backend_model": "",
  "backend_timeout_sec": 60
}
```

- `inference_backend`：`auto`（自动探测）/ `ollama` / `vllm` / `llamacpp`
- `backend_base_url`：推理引擎地址
- `backend_model`：留空表示用云端派单指定的模型
- `backend_timeout_sec`：引擎超时秒数
- `backend_vllm_base_url` / `backend_llamacpp_base_url`：**OpenAI 兼容**引擎（vLLM / llama.cpp）地址；留空 ⇒ **不参与自动探测**，避免把 8000 端口上的无关服务误认成推理引擎
- `backend_api_key`：OpenAI 兼容引擎的可选 API Key（`--api-key`）
- 查看状态：`curl http://127.0.0.1:52415/health` 的 `inference` 字段会告诉你当前生效的引擎与可用模型

---

## 🧲 模型管理（免命令行） | Model Management Without a Console

**[EN]** In the panel (**http://127.0.0.1:52415/ui**) open the **「本地 Ollama 模型管理」** block:

| Action | What happens |
|---|---|
| **一键拉取并绑定** | Downloads the model through your local Ollama in a background thread, then writes it to `agent.config.json` (`backend_model` + `model_name`) — the node starts serving it on the next dispatch |
| **设为当前接单模型** | Switches to an already-installed model, effective immediately (no restart) |

**[CN]** 在面板（**http://127.0.0.1:52415/ui**）打开 **「本地 Ollama 模型管理」** 区块：

| 操作 | 效果 |
|---|---|
| **一键拉取并绑定** | 通过本机 Ollama 后台线程下载模型，随后写进 `agent.config.json`（`backend_model` + `model_name`）—— 下一次派单即用它接单 |
| **设为当前接单模型** | 切换到已装好的模型，**立即生效**（不必重启） |

> Ollama 没启动时面板会如实提示「本机 Ollama 未运行」，而不是报错；装好 Ollama 后自动识别。

---

## 🧬 云端黄金记忆本地模型进化 | One-Click Model Evolution

**[EN]** Open the **「🧬 云端黄金记忆本地模型进化」** block in the panel
(**http://127.0.0.1:52415/ui**):

| Action | What happens |
|---|---|
| **一键合成进化模型** | Fetches the cloud's curated golden memory as an Ollama `Modelfile`, then calls your local Ollama `POST /api/create` to synthesise a derived model (e.g. `qwen2.5-apipaw-golden:latest`) |
| **合成后立即设为当前接单模型** | Also writes the new model into `agent.config.json` (`backend_model` + `model_name`) so the node serves it from the next dispatch on |

The panel shows how many golden memories the cloud holds before you start. The
base model defaults to your current `backend_model` (or `qwen2.5:0.5b`), and the
new name is derived from it automatically — leave the field empty and it just
works.

**[CN]** 在面板（**http://127.0.0.1:52415/ui**）打开 **「🧬 云端黄金记忆本地模型进化」** 区块：

| 操作 | 效果 |
|---|---|
| **一键合成进化模型** | 从云端拉取业主策展的黄金记忆（标准 Ollama `Modelfile`），交给本机 Ollama 的 `POST /api/create` 合成派生模型（如 `qwen2.5-apipaw-golden:latest`） |
| **合成后立即设为当前接单模型** | 同时把新模型写进 `agent.config.json`（`backend_model` + `model_name`）—— 下一次派单即用它接单 |

面板会先告诉您云端当前有多少条黄金记忆。基准模型默认取您当前的
`backend_model`（没有就取 `qwen2.5:0.5b`），模型名留空会自动推导。

> 云端下发的 Modelfile 里只有**已脱敏**的公开语料；节点只解析 `FROM` /
> `PARAMETER` / `SYSTEM` / `MESSAGE` 四类指令，远端文本里的其它内容一律忽略。

---

## 🆘 Troubleshooting | 常见问题

**[EN]**

| Symptom | Fix |
|---|---|
| `install.bat` flashes and closes | Python is missing, or `Add Python to PATH` was not ticked — reinstall Python 3.10+ with that option |
| `UnicodeDecodeError` during install | Your system locale is GBK — use `install_cn.bat` (it forces UTF-8 mode) or run `set PYTHONUTF8=1` first |
| The browser does not open | Open `http://127.0.0.1:52415/ui` manually |
| Panel keeps saying "not connected" | Check `Node ID` / `Node Secret` — they are case-sensitive and must not contain spaces |
| "节点不可用" (node unavailable) | The credential was revoked on the cloud side — issue a new one from the console |

**[CN]**

| 现象 | 处理办法 |
|---|---|
| 双击 `install.bat` 闪退 | 没装 Python，或安装时没勾 `Add Python to PATH`；重装 Python 3.10+ 并补勾 |
| 安装时报 `UnicodeDecodeError` | 系统默认 GBK 编码导致，请改用 `install_cn.bat`（已强制 UTF-8 模式），或先执行 `set PYTHONUTF8=1` |
| pip 下载很慢或超时 | 改用 `install_cn.bat`（清华 TUNA 镜像源） |
| 浏览器没有自动弹出面板 | 手动打开 `http://127.0.0.1:52415/ui` |
| 面板一直显示「未连接」 | 检查 `Node ID` / `Node Secret` 是否填错（区分大小写、不要有空格） |
| 提示「节点不可用」 | 凭证已被云端吊销，请到控制台重新签发 |

---

## 🔄 Run 24/7 | 开机自启（长期挂机）

**[EN]** Press `Win + R`, type `shell:startup` and press Enter, then drop a shortcut to `start_silent.vbs` into the folder that opens.

**[CN]** 按 `Win + R` → 输入 `shell:startup` 回车 → 把 `start_silent.vbs` 的快捷方式拖进打开的文件夹即可。

---

许可证：MIT License。模型元数据规范见 [`PAWMODEL.md`](https://github.com/apipaw/apipaw-agent/blob/v{{VERSION}}/PAWMODEL.md)。
