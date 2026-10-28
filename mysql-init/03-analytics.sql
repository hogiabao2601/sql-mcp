-- A second database, so multi-database tools and per-profile defaults have something to test.
-- `app_users` deliberately has secret-looking columns to demonstrate column masking.
CREATE DATABASE analytics;
USE analytics;

CREATE TABLE app_users (
  id            INT PRIMARY KEY AUTO_INCREMENT,
  email         VARCHAR(120) NOT NULL,
  password_hash CHAR(60)     NOT NULL COMMENT 'bcrypt hash',
  api_token     CHAR(32)     NOT NULL,
  created_at    DATETIME     NOT NULL,
  UNIQUE KEY uq_app_users_email (email)
);

CREATE TABLE page_views (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id    INT          NULL,
  path       VARCHAR(200) NOT NULL,
  viewed_at  DATETIME     NOT NULL,
  KEY idx_page_views_viewed_at (viewed_at),
  FOREIGN KEY (user_id) REFERENCES app_users(id)
);

INSERT INTO app_users (email, password_hash, api_token, created_at) VALUES
  ('ann@example.test',  '$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'tok_ann_0123456789abcdef012345', NOW() - INTERVAL 30 DAY),
  ('bob@example.test',  '$2b$12$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tok_bob_0123456789abcdef012345', NOW() - INTERVAL 20 DAY),
  ('cara@example.test', '$2b$12$ccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'tok_cara_123456789abcdef012345', NOW() - INTERVAL 10 DAY);

INSERT INTO page_views (user_id, path, viewed_at)
WITH RECURSIVE n (i) AS (
  SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 120
)
SELECT IF(i % 5 = 0, NULL, 1 + (i % 3)),
       ELT(1 + (i % 4), '/home', '/pricing', '/docs', '/checkout'),
       NOW() - INTERVAL (i % 5) DAY - INTERVAL ((i * 53) % 1300) MINUTE
FROM n;

GRANT SELECT ON analytics.* TO 'readonly_user'@'%';
