-- Schema for the `shop` test database. Runs once, as root, on first start of an empty volume.
USE shop;

CREATE TABLE customers (
  id         INT PRIMARY KEY AUTO_INCREMENT,
  name       VARCHAR(60)  NOT NULL,
  email      VARCHAR(120) NOT NULL,
  city       VARCHAR(60)  NOT NULL,
  created_at DATETIME     NOT NULL
);

CREATE TABLE products (
  id       INT PRIMARY KEY AUTO_INCREMENT,
  name     VARCHAR(80)   NOT NULL,
  category VARCHAR(40)   NOT NULL,
  price    DECIMAL(10,2) NOT NULL
);

CREATE TABLE orders (
  id          INT PRIMARY KEY AUTO_INCREMENT,
  customer_id INT           NOT NULL,
  status      ENUM('paid','shipped','cancelled') NOT NULL,
  total       DECIMAL(10,2) NOT NULL DEFAULT 0,
  created_at  DATETIME      NOT NULL,
  FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE order_items (
  id         INT PRIMARY KEY AUTO_INCREMENT,
  order_id   INT           NOT NULL,
  product_id INT           NOT NULL,
  quantity   INT           NOT NULL,
  unit_price DECIMAL(10,2) NOT NULL,
  FOREIGN KEY (order_id)   REFERENCES orders(id),
  FOREIGN KEY (product_id) REFERENCES products(id)
);

-- Accounts. We do NOT use the image's MYSQL_USER variable because it grants ALL on shop.
--   readonly_user : SELECT only. This is the account to register with the MCP server.
--   app_writer    : can write. Only for proving that the server's READ ONLY layer blocks writes.
CREATE USER 'readonly_user'@'%' IDENTIFIED BY 'readonly-pw-test-only';
GRANT SELECT ON shop.* TO 'readonly_user'@'%';

CREATE USER 'app_writer'@'%' IDENTIFIED BY 'writer-pw-test-only';
GRANT SELECT, INSERT, UPDATE, DELETE ON shop.* TO 'app_writer'@'%';
