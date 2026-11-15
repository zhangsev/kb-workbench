"""Creates a self-contained demo: two small Java services that call each other,
an LLMWiki-layout knowledge base with raw material, and a config pointing at them."""
from __future__ import annotations

from pathlib import Path

from .util import git, log, run

FILES = {
    "repos/order-service/pom.xml": "<project><artifactId>order-service</artifactId></project>\n",
    "repos/order-service/src/main/resources/application.yml": """order:
  timeout-minutes: 30
inventory:
  base-url: http://inventory-service:8082
""",
    "repos/order-service/src/main/java/com/example/order/OrderController.java": """package com.example.order;

/** HTTP entry point: POST /api/orders creates an order. */
public class OrderController {
    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    // POST /api/orders
    public Order createOrder(String sku, int quantity) {
        return orderService.createOrder(sku, quantity);
    }

    // POST /api/orders/{id}/cancel
    public void cancelOrder(String orderId) {
        orderService.cancelOrder(orderId);
    }
}
""",
    "repos/order-service/src/main/java/com/example/order/OrderService.java": """package com.example.order;

/** Creates and cancels orders. Stock is reserved in inventory-service. */
public class OrderService {
    public static final String TOPIC_ORDER_CREATED = "order.created";
    public static final String TOPIC_ORDER_CANCELLED = "order.cancelled";

    private final InventoryClient inventoryClient;
    private final OrderEventPublisher publisher;

    public OrderService(InventoryClient inventoryClient, OrderEventPublisher publisher) {
        this.inventoryClient = inventoryClient;
        this.publisher = publisher;
    }

    public Order createOrder(String sku, int quantity) {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity must be positive");
        }
        boolean reserved = inventoryClient.reserveStock(sku, quantity);
        if (!reserved) {
            throw new IllegalStateException("insufficient stock for " + sku);
        }
        Order order = new Order(sku, quantity, OrderStatus.PENDING_PAYMENT);
        publisher.publish(TOPIC_ORDER_CREATED, order);
        return order;
    }

    /** Called by the timeout job after order.timeout-minutes without payment. */
    public void cancelOrder(String orderId) {
        Order order = Order.load(orderId);
        if (order.getStatus() != OrderStatus.PENDING_PAYMENT) {
            return;
        }
        order.setStatus(OrderStatus.CANCELLED);
        inventoryClient.releaseStock(order.getSku(), order.getQuantity());
        publisher.publish(TOPIC_ORDER_CANCELLED, order);
    }
}
""",
    "repos/order-service/src/main/java/com/example/order/InventoryClient.java": """package com.example.order;

/** HTTP client for inventory-service. */
public class InventoryClient {
    // POST /api/inventory/reserve
    public boolean reserveStock(String sku, int quantity) {
        return HttpJson.post("/api/inventory/reserve", sku, quantity);
    }

    // POST /api/inventory/release
    public boolean releaseStock(String sku, int quantity) {
        return HttpJson.post("/api/inventory/release", sku, quantity);
    }
}
""",
    "repos/order-service/src/main/java/com/example/order/Order.java": """package com.example.order;

public class Order {
    private final String sku;
    private final int quantity;
    private OrderStatus status;

    public Order(String sku, int quantity, OrderStatus status) {
        this.sku = sku;
        this.quantity = quantity;
        this.status = status;
    }

    public static Order load(String orderId) {
        return new Order("demo", 1, OrderStatus.PENDING_PAYMENT);
    }

    public String getSku() { return sku; }
    public int getQuantity() { return quantity; }
    public OrderStatus getStatus() { return status; }
    public void setStatus(OrderStatus status) { this.status = status; }
}
""",
    "repos/order-service/src/main/java/com/example/order/OrderStatus.java": """package com.example.order;

public enum OrderStatus { PENDING_PAYMENT, PAID, CANCELLED }
""",
    "repos/order-service/src/main/java/com/example/order/OrderEventPublisher.java": """package com.example.order;

public class OrderEventPublisher {
    public void publish(String topic, Order order) {
        System.out.println("publish " + topic);
    }
}
""",
    "repos/order-service/src/main/java/com/example/order/HttpJson.java": """package com.example.order;

final class HttpJson {
    static boolean post(String path, String sku, int quantity) {
        return true;
    }
}
""",
    "repos/inventory-service/pom.xml": "<project><artifactId>inventory-service</artifactId></project>\n",
    "repos/inventory-service/src/main/java/com/example/inventory/InventoryController.java": """package com.example.inventory;

public class InventoryController {
    private final StockService stockService;

    public InventoryController(StockService stockService) {
        this.stockService = stockService;
    }

    // POST /api/inventory/reserve
    public boolean reserve(String sku, int quantity) {
        return stockService.reserve(sku, quantity);
    }

    // POST /api/inventory/release
    public boolean release(String sku, int quantity) {
        return stockService.release(sku, quantity);
    }
}
""",
    "repos/inventory-service/src/main/java/com/example/inventory/StockService.java": """package com.example.inventory;

import java.util.HashMap;
import java.util.Map;

/** Keeps available and reserved stock per SKU. */
public class StockService {
    private final Map<String, Integer> available = new HashMap<>();
    private final Map<String, Integer> reserved = new HashMap<>();

    public synchronized boolean reserve(String sku, int quantity) {
        int free = available.getOrDefault(sku, 0);
        if (free < quantity) {
            return false;
        }
        available.put(sku, free - quantity);
        reserved.merge(sku, quantity, Integer::sum);
        return true;
    }

    public synchronized boolean release(String sku, int quantity) {
        int held = reserved.getOrDefault(sku, 0);
        if (held < quantity) {
            return false;
        }
        reserved.put(sku, held - quantity);
        available.merge(sku, quantity, Integer::sum);
        return true;
    }
}
""",
    "repos/inventory-service/src/main/java/com/example/inventory/OrderCancelledListener.java": """package com.example.inventory;

/** Consumes topic order.cancelled as a safety net for missed release calls. */
public class OrderCancelledListener {
    public static final String TOPIC = "order.cancelled";
    private final StockService stockService;

    public OrderCancelledListener(StockService stockService) {
        this.stockService = stockService;
    }

    public void onMessage(String sku, int quantity) {
        stockService.release(sku, quantity);
    }
}
""",
    "kb/.wiki/AGENTS.md": "# AGENTS.md\n\nDemo wiki in the LLM Wiki layout: raw/ holds source material, wiki/ holds pages.\n",
    "kb/.wiki/raw/order-timeout-design.md": """# 订单超时取消设计评审纪要

日期：2026-03-12

结论：
1. 订单创建后 30 分钟未支付，由定时任务调用 OrderService.cancelOrder 取消。
2. 取消时同步调用库存服务 /api/inventory/release 释放预占库存。
3. 同时发布 order.cancelled 消息，库存服务消费该消息做兜底释放。
4. 只有 PENDING_PAYMENT 状态的订单可以被超时取消。
""",
    "kb/.wiki/raw/incident-2026-05-stock-leak.md": """# 故障记录：库存预占未释放

现象：部分 SKU 可售库存持续减少，但没有对应的已支付订单。
原因：订单取消时库存服务短暂不可用，releaseStock 调用失败后没有重试。
处理：新增 OrderCancelledListener 消费 order.cancelled 做兜底释放。
""",
    "kb/.wiki/wiki/index.md": """# Wiki Index

## Entities

- [订单服务](entities/order-service.md)
- [库存服务](entities/inventory-service.md)

## Concepts

- [订单超时取消](concepts/order-timeout-cancel.md)
- [库存预占泄漏排障](concepts/stock-leak-troubleshooting.md)

## Sources

- [订单超时取消设计评审纪要](sources/order-timeout-design-summary.md)
- [故障记录：库存预占未释放](sources/incident-2026-05-stock-leak-summary.md)
""",
    "kb/.wiki/wiki/log.md": "# Log\n",
    "kb/.wiki/wiki/sources/order-timeout-design-summary.md": """---
type: summary
title: 订单超时取消设计评审纪要（摘要）
source_path: raw/order-timeout-design.md
ingested: 2026-03-13
updated: 2026-03-13
---
# 订单超时取消设计评审纪要（摘要）

订单 30 分钟未支付自动取消，取消时释放库存并发布 order.cancelled 消息。
""",
    "kb/.wiki/wiki/sources/incident-2026-05-stock-leak-summary.md": """---
type: summary
title: 故障记录：库存预占未释放（摘要）
source_path: raw/incident-2026-05-stock-leak.md
ingested: 2026-05-20
updated: 2026-05-20
---
# 故障记录：库存预占未释放（摘要）

取消订单时库存释放调用失败且无重试，导致预占库存泄漏。
""",
    "kb/.wiki/wiki/concepts/order-timeout-cancel.md": """---
type: concept
title: 订单超时取消
tags: [订单, 超时, 库存]
sources:
  - sources/order-timeout-design-summary.md
created: 2026-03-13
updated: 2026-05-21
---
# 订单超时取消

## 规则

订单创建后 30 分钟未支付会被自动取消，配置项为 `order.timeout-minutes`。
只有待支付（PENDING_PAYMENT）状态的订单可以被取消。

## 流程

1. 定时任务调用 `OrderService.cancelOrder`。
2. 订单服务调用库存服务 `/api/inventory/release` 释放预占库存。
3. 订单服务发布 `order.cancelled` 消息，[库存服务](../entities/inventory-service.md) 消费后做兜底释放。
""",
    "kb/.wiki/wiki/concepts/stock-leak-troubleshooting.md": """---
type: concept
title: 库存预占泄漏排障
tags: [库存, 排障]
sources:
  - sources/incident-2026-05-stock-leak-summary.md
created: 2026-05-20
updated: 2026-05-20
---
# 库存预占泄漏排障

## 现象

可售库存持续减少，但没有对应的已支付订单。

## 排查步骤

1. 对比订单服务的取消记录与库存服务的释放记录。
2. 检查 `InventoryClient.releaseStock` 调用是否失败。
3. 确认 `OrderCancelledListener` 是否正常消费 `order.cancelled`。
""",
    "kb/.wiki/wiki/entities/order-service.md": """---
type: entity
title: 订单服务
tags: [订单, 服务知识卡]
sources:
  - sources/order-timeout-design-summary.md
created: 2026-03-13
updated: 2026-05-21
---
# 订单服务

职责：创建订单、取消订单。创建时调用库存服务预占库存。

- 包名前缀：`com.example.order`
- 对外接口：`POST /api/orders`、`POST /api/orders/{id}/cancel`
- 发布消息：`order.created`、`order.cancelled`
- 下游依赖：[库存服务](inventory-service.md)
""",
    "kb/.wiki/wiki/entities/inventory-service.md": """---
type: entity
title: 库存服务
tags: [库存, 服务知识卡]
created: 2026-03-13
---
# 库存服务

职责：维护每个 SKU 的可售库存与预占库存。

- 包名前缀：`com.example.inventory`
- 对外接口：`POST /api/inventory/reserve`、`POST /api/inventory/release`
- 消费消息：`order.cancelled`
""",
}

CONFIG = """# Demo configuration written by `kbw setup --examples`. Paths are relative to this file.
data_dir: .kbw
offline: false

knowledge:
  wiki_root: examples/kb/.wiki
  source_dirs:
    - examples/kb/.wiki/raw

models:
  dir: models
  embedding: BAAI/bge-small-zh-v1.5
  reranker: BAAI/bge-reranker-base
  rerank_enabled: false

repos:
  - name: order-service
    path: examples/repos/order-service
    package_prefixes: [com.example.order]
    keywords: [订单, 下单, order]
    depends_on: [inventory-service]
    topics: [order.created, order.cancelled]
  - name: inventory-service
    path: examples/repos/inventory-service
    package_prefixes: [com.example.inventory]
    keywords: [库存, 预占, inventory, stock]
    depends_on: []
    topics: [order.cancelled]
"""


def create(root: Path, config_path: Path) -> None:
    ex = root / "examples"
    for rel, content in FILES.items():
        p = ex / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    for name in ("order-service", "inventory-service"):
        repo = ex / "repos" / name
        if not (repo / ".git").exists():
            try:
                run(["git", "init", "-q", str(repo)])
                git(repo, "add", "-A")
                git(repo, "-c", "user.name=kbw-demo", "-c", "user.email=kbw-demo@example.invalid",
                    "commit", "-q", "-m", "demo")
            except FileNotFoundError:
                log.warning("git not found: demo repos are not git repositories, commits will be unknown")
    if not config_path.exists():
        config_path.write_text(CONFIG, encoding="utf-8")
        log.info("wrote %s", config_path)
    log.info("demo data ready under %s", ex)
