SET ANSI_NULLS ON;
GO
SET QUOTED_IDENTIFIER ON;
GO
CREATE VIEW dbo.vw_CustomerSummary
AS
SELECT  c.customer_id,
        dbo.clr_ToTitleCase(c.first_name) AS first_name_display,
        dbo.clr_ToTitleCase(c.last_name)  AS last_name_display
FROM    dbo.customers c;
GO
