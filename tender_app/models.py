from pydantic import BaseModel, Field


class TenderRecord(BaseModel):
    """

    Standardized tender information record.

    """

    title: str = Field(
        default="",
        description="Tender project title",
    )

    purchaser: str = Field(
        default="",
        description="Purchaser or tendering organization",
    )

    agency: str = Field(
        default="",
        description="Tender agency",
    )

    region: str = Field(
        default="",
        description="Region where the tender project is located",
    )

    industry: str = Field(
        default="",
        description="Industry or project category",
    )

    notice_type: str = Field(
        default="",
        description="Notice type, such as tender notice or award notice",
    )

    publish_date: str = Field(
        default="",
        description="Publication date",
    )

    deadline: str = Field(
        default="",
        description="Tender submission deadline",
    )

    budget: str = Field(
        default="",
        description="Project budget",
    )

    winner: str = Field(
        default="",
        description="Winning company",
    )

    winning_amount: str = Field(
        default="",
        description="Winning amount",
    )

    contact: str = Field(
        default="",
        description="Contact person",
    )

    phone: str = Field(
        default="",
        description="Contact phone number",
    )

    summary: str = Field(
        default="",
        description="详情页公告摘要",
    )

    content: str = Field(default="", description="详情页公告正文")

    source_name: str = Field(
        default="",
        description="Name of the source website",
    )

    source_url: str = Field(
        default="",
        description="Original tender notice URL",
    )

    collected_at: str = Field(
        default="",
        description="Time when the record was collected",
    )
