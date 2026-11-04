CREATE OR REPLACE FUNCTION `{{ catalog }}`.`{{ schema }}`.`monthly_revenue`()
RETURNS TABLE (order_month STRING, revenue DECIMAL(18, 2))
LANGUAGE SQL
COMMENT 'Certified synthetic completed order revenue by month'
RETURN
SELECT order_month, CAST(SUM(amount) AS DECIMAL(18, 2)) AS revenue
FROM `{{ catalog }}`.`{{ schema }}`.`orders`
WHERE status = 'completed'
GROUP BY order_month
ORDER BY order_month;

CREATE OR REPLACE FUNCTION `{{ catalog }}`.`{{ schema }}`.`resolution_hours`()
RETURNS TABLE (mean_resolution_hours DOUBLE)
LANGUAGE SQL
COMMENT 'Certified synthetic mean resolution hours for resolved tickets'
RETURN
SELECT AVG(resolution_hours) AS mean_resolution_hours
FROM `{{ catalog }}`.`{{ schema }}`.`support_tickets`
WHERE status = 'resolved';

CREATE OR REPLACE FUNCTION `{{ catalog }}`.`{{ schema }}`.`conversion_rate`()
RETURNS TABLE (channel STRING, conversion_rate DOUBLE)
LANGUAGE SQL
COMMENT 'Certified synthetic completed purchases divided by sessions'
RETURN
SELECT channel, CAST(SUM(purchases) AS DOUBLE) / SUM(sessions) AS conversion_rate
FROM `{{ catalog }}`.`{{ schema }}`.`campaign_daily`
GROUP BY channel
ORDER BY channel;
