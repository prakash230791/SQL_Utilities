SET ANSI_NULLS ON;
GO
SET QUOTED_IDENTIFIER ON;
GO
CREATE PROCEDURE dbo.usp_GetCustomerDisplayName
    @CustomerId INT
AS
BEGIN
    SET NOCOUNT ON;
    SELECT  c.customer_id,
            dbo.clr_ToTitleCase(c.first_name + N' ' + c.last_name) AS display_name
    FROM    dbo.customers c
    WHERE   c.customer_id = @CustomerId;
END
GO
