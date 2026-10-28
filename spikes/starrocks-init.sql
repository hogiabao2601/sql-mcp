-- StarRocks port of the test seed (spike subset). Single BE, so replication_num = 1.
CREATE DATABASE IF NOT EXISTS shop;
CREATE DATABASE IF NOT EXISTS analytics;

CREATE TABLE shop.orders (id INT NOT NULL, customer_id INT NOT NULL, status VARCHAR(10) NOT NULL,
                          total DECIMAL(10,2) NOT NULL, created_at DATETIME NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
CREATE TABLE shop.order_items (id INT NOT NULL, order_id INT NOT NULL, quantity INT NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
CREATE TABLE analytics.app_users (id INT NOT NULL, email VARCHAR(120) NOT NULL, password_hash VARCHAR(60) NOT NULL,
                                  api_token VARCHAR(32) NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");

INSERT INTO shop.orders SELECT generate_series, 1 + generate_series % 10, 'paid', 10 * generate_series, now()
  FROM TABLE(generate_series(1, 40));
INSERT INTO shop.order_items SELECT generate_series, 1 + generate_series % 40, 1 + generate_series % 3
  FROM TABLE(generate_series(1, 90));
INSERT INTO analytics.app_users VALUES
  (1, 'ann@example.test', 'aaaa', 'tok_ann_0123456789abcdef012345'),
  (2, 'bob@example.test', 'bbbb', 'tok_bob_0123456789abcdef012345');

CREATE USER readonly_user IDENTIFIED BY 'readonly-pw-test-only';
GRANT SELECT ON ALL TABLES IN DATABASE shop TO USER readonly_user;
GRANT SELECT ON ALL TABLES IN DATABASE analytics TO USER readonly_user;
CREATE USER app_writer IDENTIFIED BY 'writer-pw-test-only';
GRANT SELECT, INSERT, DELETE, UPDATE ON ALL TABLES IN DATABASE shop TO USER app_writer;
