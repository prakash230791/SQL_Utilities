SET ANSI_NULLS ON;
GO
SET QUOTED_IDENTIFIER ON;
GO
CREATE PROCEDURE dbo.usp_FormatAddressLabel
    @AddressId INT
AS
BEGIN
    SET NOCOUNT ON;
    SELECT  a.address_id,
            dbo.clr_ToTitleCase(a.city) AS city_display,
            dbo.clr_ToTitleCase(a.street_line1) + N', ' + dbo.clr_ToTitleCase(a.city) AS full_label
    FROM    dbo.addresses a
    WHERE   a.address_id = @AddressId;
END
GO
