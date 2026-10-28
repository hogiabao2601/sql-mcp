-- Legitimate but awkward names (hyphen, space, non-ASCII) for identifier handling tests.
-- A separate database, so the shop/analytics expectations of tests/smoke_mysql.py stay unchanged.
CREATE DATABASE `odd-names` CHARACTER SET utf8mb4;
USE `odd-names`;
CREATE TABLE `order-archive` (id INT PRIMARY KEY, note VARCHAR(40) NOT NULL);
CREATE TABLE `Order History` (id INT PRIMARY KEY, happened_at DATETIME NOT NULL);
CREATE TABLE `café` (id INT PRIMARY KEY, `crème` VARCHAR(20) NOT NULL);
INSERT INTO `order-archive` VALUES (1, 'archived'), (2, 'archived too');
INSERT INTO `Order History` VALUES (1, NOW());
INSERT INTO `café` VALUES (1, 'brûlée');
GRANT SELECT ON `odd-names`.* TO 'readonly_user'@'%';
